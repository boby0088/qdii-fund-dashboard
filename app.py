# -*- coding: utf-8 -*-
"""
app.py — 本地 Web 服务（纯标准库 http.server）

路由：
  GET  /               → Web 仪表盘
  GET  /api/data       → 最近一次快照（若快照超过 10 分钟则自动后台刷新）
  POST /api/refresh    → 立即刷新并返回最新快照
  GET  /api/status     → 快照时间 / 端口 / 版本信息
仅绑定 127.0.0.1。
"""
import json
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pipeline

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
AUTO_REFRESH_AFTER = 600  # 秒

_refresh_lock = threading.Lock()
_refreshing = threading.Event()


def _snap_ts(snap):
    """快照生成时间的 unix 时间戳；解析失败返回 0。"""
    try:
        return datetime.strptime(snap["generated_at"], "%Y-%m-%d %H:%M:%S").timestamp()
    except (KeyError, TypeError, ValueError):
        return 0.0


def _refresh_now():
    """串行化执行全量刷新（防止并发写同一快照文件）。"""
    with _refresh_lock:
        return pipeline.run_refresh()


def _refresh_async():
    """后台刷新：防抖（同一时刻只允许一个刷新线程）。"""
    if _refreshing.is_set():
        return
    _refreshing.set()

    def worker():
        try:
            _refresh_now()
        except Exception:  # noqa: BLE001
            pass  # 失败不阻塞，下次请求会再次触发
        finally:
            _refreshing.clear()

    threading.Thread(target=worker, daemon=True).start()


def _get_data(force=False):
    snap = pipeline.load_snapshot()
    if force:
        try:
            return _refresh_now()
        except Exception as e:  # noqa: BLE001
            if not snap:
                raise
            snap = dict(snap)
            snap["warnings"] = (snap.get("warnings") or []) + [f"刷新失败: {e}"]
            return snap

    stale = not snap or (time.time() - _snap_ts(snap) > AUTO_REFRESH_AFTER)
    if stale:
        if snap:
            # 有旧数据 → 立即返回，同时后台刷新，避免页面卡 30 秒
            out = dict(snap)
            out["warnings"] = (snap.get("warnings") or []) + ["数据超过 10 分钟，正在后台刷新…"]
            _refresh_async()
            return out
        # 完全没有快照 → 只能阻塞首次刷新
        try:
            return _refresh_now()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"首次刷新失败: {e}") from e
    return snap


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html_path = os.path.join(WEB_DIR, "index.html")
            if not os.path.exists(html_path):
                self._send(404, "index.html not found", "text/plain; charset=utf-8")
                return
            with open(html_path, encoding="utf-8") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/data":
            try:
                self._send_json(_get_data(force=False))
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, 500)
        elif path == "/api/status":
            snap = pipeline.load_snapshot()
            self._send_json({
                "generated_at": (snap or {}).get("generated_at"),
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "fees_updated": (snap or {}).get("fees_updated"),
            })
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/refresh":
            try:
                snap = _get_data(force=True)
                self._send_json({"ok": True, "data": snap})
            except Exception as e:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(e)}, 500)
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")


def main(port=8765):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"QDII 定投雷达 已启动: http://127.0.0.1:{port}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
