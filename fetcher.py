# -*- coding: utf-8 -*-
"""
fetcher.py — 东方财富 / 天天基金数据抓取模块（纯标准库）

数据源：
  1) 场内 ETF 实时行情   : push2.eastmoney.com (clist, fs=b:MK0023 跨境/QDII板块)
  2) 场外申购限额/状态   : fund.eastmoney.com/{code}.html (交易状态 文本)
  3) 历史净值(跟踪偏离)  : api.fund.eastmoney.com/f10/lsjz (pageSize 上限 20)
  4) 费率(兜底用)        : fundf10.eastmoney.com/jjfl_{code}.html
"""
import gzip
import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

PUSH2_HOSTS = [
    "http://push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
]
LSJZ_REFERER = "http://fundf10.eastmoney.com/"
FUND_PAGE_REFERER = "http://fund.eastmoney.com/"

NAV_PAGE_SIZE = 20          # lsjz 接口单页上限
NAV_DAYS_TARGET = 130       # 需要约130个交易日(≈6个月)的净值做跟踪偏离
NAV_MAX_PAGES = 8


def http_get(url, referer=None, timeout=20, tries=3):
    """通用 GET，支持 gzip 解压与重试。"""
    headers = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if referer:
        headers["Referer"] = referer
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


def http_get_json(url, referer=None, timeout=20, tries=3):
    return json.loads(http_get(url, referer, timeout, tries).decode("utf-8", "ignore"))


# ---------------------------------------------------------------------------
# 场内 ETF 行情
# ---------------------------------------------------------------------------
ETF_FIELDS = "f2,f3,f5,f6,f12,f14,f23,f24,f25"


def _clist_page(host, pn, pz=100):
    url = (
        f"{host}/api/qt/clist/get?pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2"
        "&ut=bd1d9ddb04089700cf9c27f6f7426281&fid=f3&fs=b:MK0023&fields=" + ETF_FIELDS
    )
    return http_get_json(url, referer="https://quote.eastmoney.com/")


def fetch_etf_spot_all():
    """分页抓取跨境/QDII板块全部行情，返回 {code: {...}}"""
    out = {}
    last = None
    for host in PUSH2_HOSTS:
        try:
            first = _clist_page(host, 1)
            total = (first.get("data") or {}).get("total") or 0
            pages = total // 100 + (1 if total % 100 else 0)
            for pn in range(1, pages + 1):
                j = _clist_page(host, pn) if pn > 1 else first
                diff = (j.get("data") or {}).get("diff") or []
                for it in diff:
                    code = str(it.get("f12", ""))
                    if not code:
                        continue
                    out[code] = {
                        "code": code,
                        "name": it.get("f14"),
                        "price": it.get("f2"),          # 最新价
                        "change_pct": it.get("f3"),     # 涨跌幅 %
                        "amount": it.get("f6"),         # 成交额 元
                    }
                if pn > 1:
                    time.sleep(0.15)
            return out
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1)
    raise last


# ---------------------------------------------------------------------------
# 历史净值
# ---------------------------------------------------------------------------
def fetch_nav_history(code, days=NAV_DAYS_TARGET, max_pages=NAV_MAX_PAGES):
    """返回 [(FSRQ, DWJZ), ...] 从新到旧，足够 days 条为止。失败抛异常。"""
    rows = []
    for page in range(1, max_pages + 1):
        url = (f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}"
               f"&pageIndex={page}&pageSize={NAV_PAGE_SIZE}")
        j = http_get_json(url, referer=LSJZ_REFERER)
        lst = (j.get("Data") or {}).get("LSJZList") or []
        if not lst:
            break
        for x in lst:
            try:
                rows.append((x["FSRQ"], float(x["DWJZ"])))
            except (KeyError, TypeError, ValueError):
                continue
        if len(rows) >= days:
            break
        time.sleep(0.1)
    return rows


def fetch_nav_latest(code):
    """最新一条单位净值，返回 (date, nav) 或 (None, None)"""
    rows = fetch_nav_history(code, days=1, max_pages=1)
    return rows[0] if rows else (None, None)


# ---------------------------------------------------------------------------
# 场外申购限额 / 状态
# ---------------------------------------------------------------------------
_LIMIT_RE = re.compile(
    r"单日[^上（(]{0,20}?上限\s*([\d,]+(?:\.\d+)?)\s*(亿元|万元|美元|元)?"
)
_LIMIT_RE2 = re.compile(r"上限\s*([\d,]+(?:\.\d+)?)\s*(亿元|万元|美元|元)")


def _parse_limit_yuan(text):
    """把 '单日累计购买上限100.00元' 之类文本解析为人民币金额；无法解析返回 None。"""
    m = _LIMIT_RE.search(text) or _LIMIT_RE2.search(text)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    unit = m.group(2) or "元"
    if unit == "亿元":
        num *= 1e8
    elif unit == "万元":
        num *= 1e4
    elif unit == "美元":
        return 0.0  # 以美元计上限, 视为不可正常申购, 由调用方显示原文
    return num


def fetch_otc_trade_status(code):
    """解析基金详情页的 交易状态 文本。

    返回: {"status": 状态词, "limit_yuan": 单日上限或None(不限)或0(不可购),
           "limit_text": 原文括号说明}
    """
    url = f"http://fund.eastmoney.com/{code}.html"
    html = http_get(url, referer=FUND_PAGE_REFERER, timeout=25).decode("utf-8", "ignore")
    i = html.find("交易状态：")
    if i < 0:
        i = html.find("交易状态:")
    if i < 0:
        return {"status": "未知", "limit_yuan": None, "limit_text": ""}
    seg = re.sub(r"<[^>]+>", " ", html[i:i + 300])
    seg = re.sub(r"\s+", " ", seg).strip()
    # 形如: 交易状态： 限大额 ( 单日累计购买上限10.00元 ) 开放赎回
    status = "未知"
    m = re.match(r"交易状态[：:]\s*([^（(]+)", seg)
    if m:
        status = re.sub(r"<[^>]+>", " ", m.group(1)).strip() or "未知"
        status = re.sub(r"\s+", " ", status).strip()
        # 部分页面状态后跟 该基金暂不开放购买/预约链接等尾巴，截断保留纯状态词
        for tail in ("该基金", "你可", "预约", "详情"):
            idx = status.find(tail)
            if idx > 0:
                status = status[:idx].strip()
                break
    paren = re.search(r"[（(]([^）)]*)[）)]", seg)
    limit_text = paren.group(1).strip() if paren else ""
    # 清理残留 HTML 与多余空白（部分页面括号内带 <a> 预约链接）
    limit_text = re.sub(r"<[^>]+>", " ", limit_text)
    limit_text = re.sub(r"\s+", " ", limit_text).strip()

    limit = _parse_limit_yuan(limit_text)
    if any(k in status for k in ("暂停", "停止", "封闭")) or "暂不开放" in seg:
        # 暂停/停止/封闭/暂不开放 申购均视为不可购买（括号内的数字可能为残留模板）
        if "暂不开放" in seg and "暂停" not in status:
            status = "暂不开放购买"
        return {"status": status, "limit_yuan": 0.0, "limit_text": limit_text}
    if "开放" in status and limit is None:
        return {"status": status, "limit_yuan": None, "limit_text": ""}
    return {"status": status, "limit_yuan": limit, "limit_text": limit_text}


# ---------------------------------------------------------------------------
# 费率（运行时兜底抓取，失败返回 None 由 watchlist 配置兜底）
# ---------------------------------------------------------------------------
def fetch_fee_live(code):
    try:
        html = http_get(f"http://fundf10.eastmoney.com/jjfl_{code}.html",
                        referer=LSJZ_REFERER).decode("utf-8", "ignore")
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        out = {}
        mg = re.search(r"管理费率\s*([\d.]+)%", text)
        cg = re.search(r"托管费率\s*([\d.]+)%", text)
        xs = re.search(r"销售服务费率\s*([\d.]+)%", text)
        if mg:
            out["mgmt"] = float(mg.group(1))
        if cg:
            out["custody"] = float(cg.group(1))
        if xs:
            out["sales"] = float(xs.group(1))
        i = text.find("申购费率")
        if i >= 0:
            seg = text[i:i + 700]
            m1 = re.search(r"小于\d+万元\s*([\d.]+)%", seg)
            if m1:
                out["sub_std"] = float(m1.group(1))
            else:
                m2 = re.search(r"申购费率[^%]{0,20}(\d+\.\d+)%", seg)
                if m2:
                    out["sub_std"] = float(m2.group(1))
        return out or None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 并发批量
# ---------------------------------------------------------------------------
def fetch_many(fn, items, workers=6, gap=0.1, desc=""):
    """并发执行 fn(item)，返回 {item: result}；异常项值为 None。"""
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, it): it for it in items}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                results[it] = fut.result()
            except Exception:  # noqa: BLE001
                results[it] = None
            if gap:
                time.sleep(gap)
    return results
