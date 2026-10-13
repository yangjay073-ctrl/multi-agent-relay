"""Web UI 冒烟：起 headless server → mock 会话跑通 → 校验各 API → 关停。"""
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PORT = int(os.environ.get("RELAY_PORT", "8978"))  # 独立端口，避免与桌面实例冲突


def call(method, path, obj=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=None if obj is None else json.dumps(obj, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def main():
    env = dict(os.environ, PYTHONIOENCODING="utf-8", RELAY_PORT=str(PORT))
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), "--headless"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env)
    try:
        for _ in range(40):
            time.sleep(0.4)
            try:
                call("GET", "/api/poll?cursor=0")
                break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError("server 启动失败（端口可能被占用：先关闭桌面实例）")
        assert call("GET", "/api/settings")["settings"]["max_rounds"] >= 1
        assert call("GET", "/api/sessions")["sessions"] is not None
        call("POST", "/api/session", {"directive": "WebUI 冒烟：修复 demo_project bug #12",
                                      "bindA": "本地Mock(测试用)", "bindB": "本地Mock(测试用)",
                                      "max_rounds": 5, "round_timeout": 60})
        logs, running = [], True
        for _ in range(60):
            time.sleep(0.8)
            d = call("GET", "/api/poll?cursor=0")
            logs, running = d["logs"], d["running"]
            if not running and logs and logs[-1].startswith("——"):
                break
        assert not running, "会话未结束"
        assert any("success" in l for l in logs), logs
        sid = [l for l in logs if l.startswith("会话 #")][-1].split("#")[1].split(" ")[0]
        rounds = call("GET", f"/api/rounds?sid={sid}")["rounds"]
        assert len(rounds) >= 3 and all(r["task_pack"] for r in rounds)
        print("OK — WebUI 冒烟通过（mock 会话经 HTTP API 完整跑通）")
    finally:
        try:
            call("POST", "/api/shutdown")
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()  # 兜底强杀，避免残留占端口
            proc.wait(timeout=10)


if __name__ == "__main__":
    main()
