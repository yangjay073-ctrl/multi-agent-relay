"""Web UI 宿主：stdlib HTTP API + 静态前端（webui_dist）+ pywebview 桌面壳。

用法：
  py server.py            启动桌面窗口（pywebview + 内嵌 HTTP 服务）
  py server.py --browser  用系统浏览器打开（开发/无 pywebview 环境备用）
"""
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8")

import settings as settings_mod
from adapters import ApiAdapter, ClipboardAdapter
from engine import Engine, Store
from mock_agent import start_mock

PORT = int(os.environ.get("RELAY_PORT", "8977"))
DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui_dist")
MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


def put_clipboard_windows(text: str):
    """Windows 剪贴板写入（ctypes）。失败只记日志，不阻塞流程。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard(0)
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(0x2002, len(data))  # GMEM_MOVEABLE|GMEM_ZEROINIT
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(13, h)  # CF_UNICODETEXT
        user32.CloseClipboard()
    except Exception as e:  # 非 Windows 或剪贴板被占用
        print(f"[clipboard] 写入失败: {e}")


class Host:
    """会话宿主状态：引擎、日志、剪贴板等待。"""

    def __init__(self):
        self.store = Store()
        self.settings = settings_mod.load()
        self.mock = None
        self.engine = None
        self.logs = []
        self.lock = threading.Lock()
        self.wait = None  # {"adapter", "token", "preview"}

    def log(self, msg):
        with self.lock:
            self.logs.append(msg)

    # ---------- 剪贴板回调 ----------
    def notify_wait(self, adapter, payload, token):
        self.wait = {"adapter": adapter, "token": token, "preview": f"{payload[:160]}…"}
        self.log(f"剪贴板模式：等待用户回传（token {token}，本轮发出 {len(payload)} 字符）…")

    def wait_done(self):
        self.wait = None

    def deliver(self, token, text):
        w = self.wait
        if not w or not w["adapter"].deliver(token, text):
            self.log("回传已丢弃：该次等待已超时或已结束")
            return False
        self.log("回传已接收 ✓")
        return True

    # ---------- 适配器 ----------
    def build(self, role, name):
        self.settings["bind"][role] = name
        if name == "本地Mock(测试用)":
            if not self.mock:
                self.mock = start_mock()
            url = self.mock[0] if role == "A" else self.mock[1]
            return ApiAdapter(url, "demo-key-not-real", "mock")
        prof = self.settings["profiles"].get(name)
        if not prof:
            raise ValueError(f"未找到绑定的 Agent 配置: {name}（请在设置页添加）")
        if prof.get("type") == "clipboard":
            return ClipboardAdapter(put_clipboard_windows, self.notify_wait,
                                    prof.get("launch_command", ""), on_done=self.wait_done)
        if not all((prof.get("base_url"), prof.get("api_key"), prof.get("model"))):
            raise ValueError(f"配置 {name} 缺少 base_url / api_key / model")
        return ApiAdapter(prof["base_url"], prof["api_key"], prof["model"])

    def start_session(self, p):
        if self.engine and any(t.is_alive() for t in threading.enumerate()
                               if getattr(t, "name", "") == "relay-engine"):
            raise ValueError("已有会话在运行，请先终止")
        directive = (p.get("directive") or "").strip()
        if not directive:
            raise ValueError("用户指令不能为空")
        a = self.build("A", p.get("bindA", ""))
        b = self.build("B", p.get("bindB", ""))
        cfg = {"max_rounds": int(p.get("max_rounds", 5)),
               "round_timeout": int(p.get("round_timeout", 600))}
        self.settings["max_rounds"], self.settings["round_timeout"] = cfg["max_rounds"], cfg["round_timeout"]
        settings_mod.save(self.settings)
        self.engine = Engine(a, b, self.store, cfg)

        def run():
            try:
                self.engine.run(directive, log=self.log)
            except Exception as e:  # 兜底：线程内异常也进日志
                self.log(f"引擎线程异常：{e!r}")
            finally:
                self.log("—— 会话线程结束 ——")

        threading.Thread(target=run, name="relay-engine", daemon=True).start()


HOST = Host()
STOP = threading.Event()


def make_handler() -> type:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # ---------- helpers ----------
        def _json(self, obj, code=200, raw=None):
            body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def _static(self, path):
            if path == "/":
                path = "/index.html"
            fp = os.path.normpath(os.path.join(DIST, path.lstrip("/")))
            if not fp.startswith(DIST) or not os.path.isfile(fp):
                return self._json({"error": "not found"}, 404)
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(os.path.splitext(fp)[1], "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---------- GET ----------
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            try:
                if u.path == "/api/poll":
                    since = int(q.get("cursor", ["0"])[0])
                    with HOST.lock:
                        logs = HOST.logs[since:]
                        cursor = len(HOST.logs)
                    running = any(t.name == "relay-engine" and t.is_alive() for t in threading.enumerate())
                    w = HOST.wait
                    return self._json({"running": running, "cursor": cursor, "logs": logs,
                                       "wait": {"active": True, "token": w["token"],
                                                "preview": w["preview"]} if w else None})
                if u.path == "/api/sessions":
                    rows = [dict(r) for r in HOST.store.sessions_list()]
                    return self._json({"sessions": rows})
                if u.path == "/api/rounds":
                    sid = int(q.get("sid", ["0"])[0])
                    rows = [{k: r[k] for k in ("round_no", "status", "stage", "duration_ms",
                                               "task_pack", "sent_to_b", "b_output", "verdict")}
                            for r in HOST.store.rounds(sid)]
                    return self._json({"rounds": rows})
                if u.path == "/api/settings":
                    return self._json({"settings": HOST.settings})
                if u.path == "/api/export":
                    sid = int(q.get("sid", ["0"])[0])
                    import tempfile
                    fd, tmp = tempfile.mkstemp(suffix=".json")
                    os.close(fd)
                    HOST.store.export_session(sid, tmp)
                    with open(tmp, "rb") as f:
                        body = f.read()
                    os.remove(tmp)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="session_{sid}_sanitized.json"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return self._static(u.path)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        # ---------- POST ----------
        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            try:
                if u.path == "/api/session":
                    HOST.start_session(self._body())
                    return self._json({"ok": True})
                if u.path == "/api/stop":
                    if HOST.engine:
                        HOST.engine.stop()
                    return self._json({"ok": True})
                if u.path == "/api/shutdown":
                    STOP.set()

                    def _quit():
                        self.server.shutdown()
                        self.server.server_close()

                    threading.Thread(target=_quit, daemon=True).start()
                    return self._json({"ok": True})
                if u.path == "/api/replan":
                    if HOST.engine:
                        HOST.engine.request_replan()
                    return self._json({"ok": True})
                if u.path == "/api/deliver":
                    b = self._body()
                    return self._json({"ok": HOST.deliver(b.get("token"), b.get("text", ""))})
                if u.path == "/api/settings":
                    HOST.settings = self._body().get("settings", HOST.settings)
                    settings_mod.save(HOST.settings)
                    return self._json({"ok": True})
                if u.path == "/api/profile_test":
                    p = self._body()
                    if p.get("type") != "api":
                        return self._json({"error": "剪贴板类无需连接测试"})
                    if not all((p.get("base_url"), p.get("api_key"), p.get("model"))):
                        return self._json({"error": "缺少 base_url / api_key / model"})
                    reply = ApiAdapter(p["base_url"], p["api_key"], p["model"]).test_connection()
                    return self._json({"ok": True, "reply": reply})
                return self._json({"error": "not found"}, 404)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

    return H


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/"
    print(f"[server] {url}")
    if "--headless" in sys.argv:      # 仅供自动化冒烟：无窗口常驻，/api/shutdown 退出
        STOP.wait()
    elif "--browser" in sys.argv:
        import webbrowser
        webbrowser.open(url)
        STOP.wait()
    else:
        import webview
        webview.create_window("多 Agent 中转协作 App", url, width=1120, height=760,
                              min_size=(880, 600))
        webview.start()
    srv.shutdown()


if __name__ == "__main__":
    main()
