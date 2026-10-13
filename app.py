"""多 Agent 中转协作 App · P0 演示控制台。

用法：
  py app.py          打开演示 UI
  py app.py --demo   无界面端到端演示（本地 mock，3 轮循环验收通过）

P0 不含正式 UI（布置页/时间线/设置页属 P1）；本窗口只是让剪贴板适配器
可交互、让引擎运行过程可见的最小载体。
"""
import queue
import sys
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk

sys.stdout.reconfigure(encoding="utf-8")

from adapters import ApiAdapter, ClipboardAdapter
from engine import Engine, Store
from mock_agent import start_mock

CHOICES = ["本地Mock(API)", "剪贴板半自动", "自定义API"]


def run_demo():
    a_url, b_url, stop = start_mock()
    logs = []
    print(f"[demo] 本地 mock Agent 已启动: A={a_url}  B={b_url}")
    engine = Engine(ApiAdapter(a_url, "demo-key-not-real", "mock-a"),
                    ApiAdapter(b_url, "demo-key-not-real", "mock-b"),
                    Store(), {"max_rounds": 5, "round_timeout": 60})
    result = engine.run("修复 demo_project 的 bug #12：用户登录后头像不刷新", log=logs.append)
    for line in logs:
        print("  " + line)
    print(f"\n[demo] 会话结果: {result['status']}，共 {result['rounds']} 轮")
    print(f"[demo] 最终产出:\n{result['final_output']}")
    print("\n[demo] SQLite 留痕（每轮任务包/产出/验收/耗时）:")
    for no, st, ms, pack, sent, b_out, verdict in engine.store.rounds(result["session_id"]):
        print(f"  轮 {no} [{st}] {ms}ms | 脱敏后发出 {len(sent)} 字符 | "
              f"B 产出 {len(b_out or '')} 字符 | 验收: {verdict[:60]}")
    stop()


class App:
    def __init__(self, root):
        self.root = root
        root.title("多 Agent 中转协作 App · P0 演示")
        root.geometry("860x680")
        self.mock = None          # (a_url, b_url, stop)，惰性启动
        self.engine = None
        self.worker = None
        self.clip_adapter = None
        self.logq = queue.Queue()
        self._build()
        root.after(100, self._poll)

    # ---------- UI ----------
    def _build(self):
        f = ttk.Frame(self.root, padding=8)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="用户指令：").pack(anchor="w")
        self.directive = tk.Text(f, height=2)
        self.directive.pack(fill="x")
        self.directive.insert("1.0", "修复 demo_project 的 bug #12：用户登录后头像不刷新")

        row = ttk.Frame(f)
        row.pack(fill="x", pady=6)
        ttk.Label(row, text="Agent A:").pack(side="left")
        self.combo_a = ttk.Combobox(row, values=CHOICES, width=16, state="readonly")
        self.combo_a.current(0)
        self.combo_a.pack(side="left", padx=(2, 12))
        ttk.Label(row, text="Agent B:").pack(side="left")
        self.combo_b = ttk.Combobox(row, values=CHOICES, width=16, state="readonly")
        self.combo_b.current(0)
        self.combo_b.pack(side="left", padx=2)
        ttk.Label(row, text="最大轮数:").pack(side="left", padx=(12, 0))
        self.max_rounds = ttk.Spinbox(row, from_=1, to=99, width=4)
        self.max_rounds.set(5)
        self.max_rounds.pack(side="left", padx=(2, 12))
        ttk.Label(row, text="单轮超时(秒):").pack(side="left")
        self.timeout = ttk.Spinbox(row, from_=30, to=7200, increment=30, width=6)
        self.timeout.set(600)
        self.timeout.pack(side="left", padx=2)

        api = ttk.Frame(f)
        api.pack(fill="x")
        ttk.Label(api, text="自定义API  base_url:").pack(side="left")
        self.api_url = ttk.Entry(api, width=28)
        self.api_url.pack(side="left", padx=(2, 10))
        ttk.Label(api, text="api_key:").pack(side="left")
        self.api_key = ttk.Entry(api, width=24, show="*")
        self.api_key.pack(side="left", padx=(2, 10))
        ttk.Label(api, text="model:").pack(side="left")
        self.api_model = ttk.Entry(api, width=14)
        self.api_model.pack(side="left", padx=2)
        ttk.Label(api, text="（仅 A 或 B 选「自定义API」时生效）").pack(side="left", padx=8)

        clip = ttk.Frame(f)
        clip.pack(fill="x")
        ttk.Label(clip, text="剪贴板模式目标程序路径(可选，如记事本):").pack(side="left")
        self.launch_cmd = ttk.Entry(clip, width=34)
        self.launch_cmd.pack(side="left", padx=2)

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=6)
        self.btn_start = ttk.Button(btns, text="启动会话", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="终止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)

        self.log = scrolledtext.ScrolledText(f, height=14, state="disabled")
        self.log.pack(fill="both", expand=True)

        self.wait = ttk.LabelFrame(f, text="剪贴板半自动 · 等待回传", padding=6)
        ttk.Label(self.wait, text="任务包已写入系统剪贴板。请切换到目标 Agent 窗口，Ctrl+V 发送，"
                                  "再把对方的回复粘贴到下面，点「回传给 App」：").pack(anchor="w")
        self.reply = tk.Text(self.wait, height=5)
        self.reply.pack(fill="x", pady=4)
        ttk.Button(self.wait, text="回传给 App", command=self.deliver).pack(anchor="e")

    def log_line(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self):
        while not self.logq.empty():
            self.log_line(self.logq.get())
        self.root.after(100, self._poll)

    # ---------- 剪贴板适配器注入 ----------
    def put_clipboard(self, text):
        def _do():
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        self.root.after(0, _do)
        self.logq.put("已把内容写入系统剪贴板（等待人工粘贴 + 回传）")
        self.root.after(0, lambda: self.wait.pack(fill="x", pady=4))

    def notify_wait(self, payload):
        self.logq.put(f"剪贴板模式：等待用户操作（本轮发出 {len(payload)} 字符）…")

    def deliver(self):
        if self.clip_adapter:
            self.clip_adapter.deliver(self.reply.get("1.0", "end").strip())
            self.root.after(0, self.wait.pack_forget)

    # ---------- 会话 ----------
    def build_adapter(self, combo):
        kind = combo.get()
        if kind == "本地Mock(API)":
            if not self.mock:
                self.mock = start_mock()
            url = self.mock[0] if combo is self.combo_a else self.mock[1]
            return ApiAdapter(url, "demo-key-not-real", "mock")
        if kind == "剪贴板半自动":
            return ClipboardAdapter(self.put_clipboard, self.notify_wait, self.launch_cmd.get().strip())
        url, key, model = self.api_url.get().strip(), self.api_key.get().strip(), self.api_model.get().strip()
        if not all((url, key, model)):
            raise ValueError("自定义API 需要填写 base_url / api_key / model")
        return ApiAdapter(url, key, model)

    def start(self):
        try:
            a, b = self.build_adapter(self.combo_a), self.build_adapter(self.combo_b)
        except ValueError as e:
            self.log_line(f"配置错误：{e}")
            return
        self.clip_adapter = next((x for x in (a, b) if isinstance(x, ClipboardAdapter)), None)
        cfg = {"max_rounds": int(self.max_rounds.get()), "round_timeout": int(self.timeout.get())}
        self.engine = Engine(a, b, Store(), cfg)

        def run():
            try:
                self.engine.run(self.directive.get("1.0", "end").strip(),
                                log=lambda m: self.logq.put(m))
            finally:
                self.logq.put("—— 会话线程结束 ——")
                self.root.after(0, lambda: (self.btn_start.configure(state="normal"),
                                            self.btn_stop.configure(state="disabled")))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")

    def stop(self):
        if self.engine:
            self.engine.stop()
        if self.clip_adapter:
            self.clip_adapter.abort()
        self.log_line("已请求终止（当前轮结束后生效；剪贴板等待立即解除）")


def ui_selftest(root, app):
    """UI 自检：自动触发一次 mock 会话，日志出现 success 即通过。py app.py --ui-selftest"""
    app.start()
    def check(n=0):
        text = app.log.get("1.0", "end")
        if "success" in text or "failed" in text:
            ok = "success" in text
            print(("UI-SELFTEST OK" if ok else "UI-SELFTEST FAIL") + " | " + text.replace("\n", " / "))
            if not ok:
                root.destroy(); sys.exit(1)
            import os
            if not os.environ.get("KEEP"):   # KEEP=1 时窗口驻留 15s 供截图
                root.destroy(); sys.exit(0)
            return
        if n > 120:
            print("UI-SELFTEST FAIL: 超时未见会话结束"); root.destroy(); sys.exit(1)
        root.after(200, lambda: check(n + 1))
    root.after(200, check)


if __name__ == "__main__":
    if "--demo" in sys.argv:
        run_demo()
    else:
        root = tk.Tk()
        app = App(root)
        if "--ui-selftest" in sys.argv:
            root.after(800, lambda: ui_selftest(root, app))
        root.mainloop()
