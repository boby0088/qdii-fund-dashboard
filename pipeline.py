# -*- coding: utf-8 -*-
"""
pipeline.py — 数据装配主流程：抓取 → 计算 → 排序 → 快照
"""
import json
import os
import time
from datetime import datetime

import fetcher
import rank

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
SNAPSHOT_PATH = os.path.join(DATA_DIR, "snapshot.json")

INDEX_LABEL = {"nasdaq100": "纳斯达克100", "sp500": "标普500",
               "globalus": "全球美股"}
W_ETFPREM = 0.5
W_MGMT = 0.3
W_TRACK = 0.2
W_OTC_MGMT = 0.6
W_OTC_TRACK = 0.4


def load_watchlist():
    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        return json.load(f)


def _fmt_money(v):
    """金额格式化：1.5亿 -> 1.50亿 / 万元 / 元"""
    if v is None:
        return "不限"
    if v == 0:
        return "不可申购"
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _fmt_amount_yi(v):
    if v is None:
        return "—"
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    return f"{v / 1e4:.0f}万"


def run_refresh():
    """执行一次完整刷新，返回快照 dict 并落盘 data/snapshot.json。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    started = time.time()
    wl = load_watchlist()
    funds = wl["funds"]
    etf_codes = [f["code"] for f in funds if f["channel"] == "etf"]
    otc_codes = [f["code"] for f in funds if f["channel"] == "otc"]
    by_code = {f["code"]: f for f in funds}
    warnings = []

    # 1) 场内行情
    spot = {}
    try:
        spot_all = fetcher.fetch_etf_spot_all()
        spot = {c: spot_all[c] for c in etf_codes if c in spot_all}
        missing = [c for c in etf_codes if c not in spot]
        if missing:
            warnings.append("场内行情未取到: " + ",".join(missing))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"场内行情抓取失败: {e}")

    # 2) 历史净值（全部基金，并发）
    navs = fetcher.fetch_many(fetcher.fetch_nav_history, [f["code"] for f in funds],
                              workers=6, gap=0.05)
    nav_fail = [c for c, v in navs.items() if not v]
    if nav_fail:
        warnings.append("净值历史为空: " + ",".join(nav_fail))

    # 3) 场外申购状态
    otc_status = fetcher.fetch_many(fetcher.fetch_otc_trade_status, otc_codes,
                                    workers=4, gap=0.15)
    otc_fail = [c for c in otc_codes if otc_status.get(c) is None]
    if otc_fail:
        warnings.append("申购状态解析失败: " + ",".join(otc_fail))

    # 4) 场内溢价率 = (最新价 - 最新净值) / 最新净值
    etf_items = []
    for c in etf_codes:
        f = by_code[c]
        rows = navs.get(c) or []
        price = spot.get(c, {}).get("price")
        amount = spot.get(c, {}).get("amount")
        change = spot.get(c, {}).get("change_pct")
        nav, nav_date = None, None
        if rows:
            nav_date, nav = rows[0]  # rows[0] = (FSRQ, DWJZ)
        premium = None
        if price and nav:
            try:
                premium = round((float(price) / float(nav) - 1.0) * 100.0, 2)
            except (TypeError, ValueError):
                premium = None
        etf_items.append({
            "code": c, "name": f["name"], "index": INDEX_LABEL.get(f["index"], f["index"]),
            "index_key": f["index"],
            "price": price, "nav": nav, "nav_date": nav_date,
            "premium_pct": premium, "change_pct": change,
            "amount": amount,
            "mgmt": f.get("mgmt"), "custody": f.get("custody"),
            "note": f.get("note", ""),
        })

    # 5) 场外组装
    otc_items = []
    for c in otc_codes:
        f = by_code[c]
        st = otc_status.get(c) or {}
        otc_items.append({
            "code": c, "name": f["name"], "index": INDEX_LABEL.get(f["index"], f["index"]),
            "index_key": f["index"],
            "status": st.get("status", "未知"),
            "limit_yuan": st.get("limit_yuan"),
            "limit_text": st.get("limit_text", ""),
            "limit_display": _fmt_money(st.get("limit_yuan")),
            "sub_std": f.get("sub_std"),
            "mgmt": f.get("mgmt"), "custody": f.get("custody"), "sales": f.get("sales"),
            # 持有费率（10年假设口径）= (管理费+托管费+销售服务费)/年 + 标准申购费率/10
            # 把一次性申购费摊到10年，使 A/C 在长期持有成本上口径一致
            "hold_cost": round(
                (f.get("mgmt") or 0.0) + (f.get("custody") or 0.0) + (f.get("sales") or 0.0)
                + (f.get("sub_std") or 0.0) / 10.0, 2),
            "note": f.get("note", ""),
        })

    # 6) 跟踪偏离
    devs = rank.compute_track_dev({c: navs.get(c) for c in navs if navs.get(c)})

    # 7) 场内评分排序
    for it in etf_items:
        it["track_dev"] = round(devs.get(it["code"]), 2) if devs.get(it["code"]) is not None else None
    scored_etf = rank.score_etf(
        {it["code"]: it["premium_pct"] for it in etf_items},
        {it["code"]: it["mgmt"] for it in etf_items},
        {it["code"]: it["track_dev"] for it in etf_items},
    )
    for it in etf_items:
        s, rp, rm, rd = scored_etf[it["code"]]
        it["score"] = round(s, 2)
        it["rank_premium"] = rp
        it["rank_mgmt"] = rm
        it["rank_track"] = rd
    etf_items.sort(key=lambda x: (x["score"], x["code"]))

    # 8) 场外过滤(限额>1 或 不限) + 评分排序
    #    limit_yuan=None 表示无申购上限（不限），视为可申购；0/<=1 被过滤
    otc_valid = [it for it in otc_items
                 if it["limit_yuan"] is None or it["limit_yuan"] > 1]
    for it in otc_valid:
        it["track_dev"] = round(devs.get(it["code"]), 2) if devs.get(it["code"]) is not None else None
    scored_otc = rank.score_otc(
        {it["code"]: it["hold_cost"] for it in otc_valid},
        {it["code"]: it["track_dev"] for it in otc_valid},
    )
    for it in otc_valid:
        s, r2, rd = scored_otc[it["code"]]
        it["score"] = round(s, 2)
        it["rank_cost"] = r2
        it["rank_mgmt"] = r2  # 兼容旧字段名（现为持有费率排名）
        it["rank_track"] = rd
    otc_valid.sort(key=lambda x: (x["score"], x["code"]))
    otc_excluded = [it for it in otc_items if it not in otc_valid]

    snapshot = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.time() - started, 1),
        "fees_updated": wl.get("fees_updated", ""),
        "etf": etf_items,
        "otc": otc_valid,
        "otc_excluded": otc_excluded,
        "warnings": warnings,
        "weights": {
            "etf": {"premium": W_ETFPREM, "mgmt": W_MGMT, "track": W_TRACK},
            "otc": {"hold_cost_10y": W_OTC_MGMT, "track": W_OTC_TRACK},
            "limit_min": 1,
        },
    }
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)
    return snapshot


def load_snapshot():
    if os.path.exists(SNAPSHOT_PATH):
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            return json.load(f)
    return None
