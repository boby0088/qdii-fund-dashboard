# -*- coding: utf-8 -*-
"""
rank.py — 跟踪偏离估算 + 综合评分排序

跟踪偏离（估算）：按指数分组，用各基金近 ~120 个交易日单位净值日收益率，
与同组中位数日收益率的差值求标准差，再年化（×√250），单位 %。
用于"相对"排序：偏离越小，说明其净值走势越贴近同类主流水平。

综合评分（越低越优，权重体现优先级）：
  场内 ETF : 0.5×溢价率排名 + 0.3×管理费率排名 + 0.2×跟踪偏离排名
  场外 A/C/D/E/I/F : 0.6×持有费率排名 + 0.4×跟踪偏离排名  （先过滤 单日限额>1元 或 不限）
  持有费率（10年假设）= (管理费+托管费+销售服务费)/年 + 标准申购费率/10
  说明：以持有10年为前提，将一次性申购费摊到10年（÷10）后与每年持有费率相加，
        使 A/C 两类（C 类无申购费但按年收销售服务费）能在"长期持有成本"同一口径下公平比较。
"""
import math
import statistics

TRACK_WINDOW = 120          # 使用最近约120个交易日
MIN_OBS = 40                # 少于该样本数视为数据不足
ANNUAL = math.sqrt(250)


def daily_returns(rows):
    """rows: [(date, nav), ...] 从新到旧 → 日收益率 dict {date: pct}（按日期从新到旧排列）。"""
    out = []
    for (d0, n0), (d1, n1) in zip(rows, rows[1:]):
        if n0 > 0:
            out.append((d0, (n0 / n1 - 1.0) * 100.0))
    return out


def compute_track_dev(navs_by_code):
    """navs_by_code: {code: [(date,nav)...]} → {code: 年化跟踪偏离% 或 None}"""
    groups = {}
    for code, rows in navs_by_code.items():
        if not rows or len(rows) < MIN_OBS + 1:
            continue
        rets = dict(daily_returns(rows))
        groups.setdefault(code, rets)

    codes = list(groups.keys())
    if len(codes) < 2:
        return {c: None for c in navs_by_code}

    # 取所有基金共同覆盖的日期（对齐）
    date_sets = [set(groups[c].keys()) for c in codes]
    common = set.intersection(*date_sets)
    common = sorted(common)  # 从新到旧
    if len(common) < MIN_OBS:
        return {c: None for c in navs_by_code}

    series = {c: [groups[c][d] for d in common] for c in codes}
    n = len(common)
    dev = {}
    for c in codes:
        bench = []
        for i in range(n):
            vals = [series[o][i] for o in codes if o != c]
            bench.append(statistics.median(vals))
        diffs = [series[c][i] - bench[i] for i in range(n)]
        dev[c] = statistics.pstdev(diffs) * ANNUAL
    for c in navs_by_code:
        dev.setdefault(c, None)
    return dev


def _rank_map(values):
    """把数值列表转为排名（1=最优）；None 排最后（并列给相同名次）。"""
    n = len(values)
    order = sorted(range(n), key=lambda i: (values[i] is None, values[i] if values[i] is not None else float("inf")))
    rank = [None] * n
    cur = 0
    prev_key = None
    for pos, i in enumerate(order):
        key = (values[i] is None, values[i])
        if key != prev_key:
            cur = pos + 1
            prev_key = key
        rank[i] = cur
    return rank


def score_etf(premiums, mgmt, devs):
    """返回 {code: (score, r_prem, r_mgmt, r_dev)}，score 越小越优"""
    codes = list(premiums.keys())
    r1 = _rank_map([premiums[c] for c in codes])
    r2 = _rank_map([mgmt[c] for c in codes])
    r3 = _rank_map([devs[c] for c in codes])
    out = {}
    for i, c in enumerate(codes):
        s = 0.5 * r1[i] + 0.3 * r2[i] + 0.2 * r3[i]
        out[c] = (s, r1[i], r2[i], r3[i])
    return out


def score_otc(hold_cost_10y, devs):
    """hold_cost_10y: {code: 持有费率%(10年口径 = 管+托+销 + 申购费/10)}，devs: {code: 跟踪偏离%}"""
    codes = list(hold_cost_10y.keys())
    r2 = _rank_map([hold_cost_10y[c] for c in codes])
    r3 = _rank_map([devs[c] for c in codes])
    out = {}
    for i, c in enumerate(codes):
        s = 0.6 * r2[i] + 0.4 * r3[i]
        out[c] = (s, r2[i], r3[i])
    return out
