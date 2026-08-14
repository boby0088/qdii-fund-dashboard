# -*- coding: utf-8 -*-
"""
run.py — QDII 定投雷达 命令行入口

用法：
  python run.py refresh          # 拉取最新数据并生成快照 + 打印简报
  python run.py serve [port]     # 启动本地 Web 仪表盘（默认端口 8765）
  python run.py snapshot         # 打印最近一次快照（不联网）
"""
import argparse
import sys


def _fmt_money(v):
    if v is None:
        return "不限"
    if v == 0:
        return "不可申购"
    if v >= 1e8:
        return f"{v/1e8:.2f}亿"
    if v >= 1e4:
        return f"{v/1e4:.2f}万"
    return f"{v:,.2f}"


def print_report(snap):
    w = snap.get("warnings") or []
    print("=" * 78)
    print(f"QDII 定投雷达 | 数据时间: {snap['generated_at']} | 耗时 {snap['elapsed_sec']}s")
    print(f"费率数据更新日: {snap.get('fees_updated','')}")
    if w:
        print("⚠ 警告:")
        for x in w:
            print("   -", x)
    print("=" * 78)

    print("\n【场内 ETF · 按 溢价率→管理费→跟踪偏离 综合排序】")
    print(f"{'排名':<4}{'代码':<8}{'名称':<16}{'指数':<12}{'最新价':>8}{'净值':>8}{'溢价率%':>9}{'涨跌%':>7}{'成交额':>10}{'管理费%':>8}{'偏离%':>8}")
    for i, it in enumerate(snap["etf"], 1):
        prem = f"{it['premium_pct']:+.2f}" if it["premium_pct"] is not None else "  —"
        dev = f"{it['track_dev']:.2f}" if it["track_dev"] is not None else "  —"
        amt = _fmt_money(it["amount"]) if it["amount"] else "—"
        print(f"{i:<4}{it['code']:<8}{it['name']:<16}{it['index']:<12}"
              f"{it['price'] or '—':>8}{it['nav'] or '—':>8}{prem:>9}"
              f"{it['change_pct'] if it['change_pct'] is not None else '—':>7}{amt:>10}"
              f"{it['mgmt'] or '—':>8}{dev:>8}")

    print("\n【场外 A/C/D/E类 · 单日限额>1元 · 按 持有费率(10年口径)→跟踪偏离 综合排序】")
    print(f"{'排名':<4}{'代码':<8}{'名称':<22}{'指数':<10}{'状态':<8}{'单日限额':>10}{'申购费%':>8}{'管+托+销%':>10}{'持有10年%':>10}{'偏离%':>8}")
    for i, it in enumerate(snap["otc"], 1):
        dev = f"{it['track_dev']:.2f}" if it["track_dev"] is not None else "  —"
        sub = it["sub_std"] if it["sub_std"] is not None else "—"
        full = round((it["mgmt"] or 0) + (it["custody"] or 0) + (it["sales"] or 0), 2)
        print(f"{i:<4}{it['code']:<8}{it['name']:<22}{it['index']:<10}{it['status']:<8}"
              f"{it['limit_display']:>10}{sub:>8}{full:>10}{it['hold_cost'] or '—':>10}{dev:>8}")

    if snap.get("otc_excluded"):
        print("\n【场外 · 当前不可申购 / 限额≤1元（已过滤）】")
        for it in snap["otc_excluded"]:
            extra = f" ({it['limit_text']})" if it.get("limit_text") else ""
            print(f"   {it['code']} {it['name']} — {it['status']}{extra}")


def cmd_refresh():
    import pipeline
    snap = pipeline.run_refresh()
    print_report(snap)
    print(f"\n快照已保存: {pipeline.SNAPSHOT_PATH}")


def cmd_snapshot():
    import pipeline
    snap = pipeline.load_snapshot()
    if not snap:
        print("暂无快照，请先运行: python run.py refresh")
        sys.exit(1)
    print_report(snap)


def cmd_serve(port):
    import app
    app.main(port)


def main():
    ap = argparse.ArgumentParser(description="QDII 定投雷达")
    ap.add_argument("cmd", nargs="?", default="serve", choices=["refresh", "serve", "snapshot"])
    ap.add_argument("port", nargs="?", type=int, default=8765)
    args = ap.parse_args()
    if args.cmd == "refresh":
        cmd_refresh()
    elif args.cmd == "snapshot":
        cmd_snapshot()
    else:
        cmd_serve(args.port)


if __name__ == "__main__":
    main()
