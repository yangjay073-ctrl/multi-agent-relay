"""多 Agent 中转协作 App · P1 正式 UI（布置页 / 轮次时间线 / 设置页）。

用法：
  py app.py                    打开 UI
  py app.py --demo             无界面端到端演示（本地 mock，3 轮循环验收通过）
  py app.py --ui-selftest      UI 自动自检
  py app.py --e2e "指令"       无界面真实端到端：按 data/settings.json 的 A/B 绑定跑
"""
import json
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

sys.stdout.reconfigure(encoding="utf-8")

import settings as settings_mod
from adapters import ApiAdapter, ClipboardAdapter
from engine import Engine, Store
from mock_agent import start_mock

MOCK_LABEL = "本地Mock(测试用)"
PROFILES_NOTE = "（在「设置」页管理候选）"


class App:
    def __init__(self, root):
        self.root = root
        root.title("多 Agent 中转协作 App")
        root.geometry("980x720")
        self.settings = settings_mod.load()
        self.mock = None
        self.engine = None
        self.clip_adapters = []
        self._waiting = None
        self.logq = queue.Queue()
        self._build()
        root.after(100, self._poll)

    # ==================== 布置页 ====================
    def _build(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True)
        self._build_setup(self.nb)
        self._build_timeline(self.nb)
        self._build_settings_tab(self.nb)

    def _build_setup(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="任务布置")

        ttk.Label(f, text="用户指令：").pack(anchor="w")
        self.directive = tk.Text(f, height=4)
        self.directive.pack(fill="x")

        row = ttk.Frame(f)
        row.pack(fill="x", pady=6)
        ttk.Label(row, text="Agent A:").pack(side="left")
        self.combo_a = ttk.Combobox(row, state="readonly", width=22)
        self.combo_a.pack(side="left", padx=(2, 14))
        ttk.Label(row, text="Agent B:").pack(side="left")
        self.combo_b = ttk.Combobox(row, state="readonly", width=22)
        self.combo_b.pack(side="left", padx=2)
        ttk.Label(row, text=PROFILES_NOTE).pack(side="left", padx=6)
        ttk.Label(row, text="最大轮数:").pack(side="left", padx=(10, 0))
        self.max_rounds = ttk.Spinbox(row, from_=1, to=99, width=4)
        self.max_rounds.set(self.settings["max_rounds"])
        self.max_rounds.pack(side="left", padx=(2, 14))
        ttk.Label(row, text="单轮超时(秒):").pack(side="left")
        self.timeout = ttk.Spinbox(row, from_=30, to=7200, increment=30, width=6)
        self.timeout.set(self.settings["round_timeout"])
        self.timeout.pack(side="left", padx=2)

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=4)
        self.btn_start = ttk.Button(btns, text="启动会话", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="终止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.btn_replan = ttk.Button(btns, text="终止本轮并重新布置", command=self.replan, state="disabled")
        self.btn_replan.pack(side="left")

        self.wait = ttk.LabelFrame(f, text="剪贴板半自动 · 等待回传", padding=6)
        ttk.Label(self.wait, text="任务包已写入系统剪贴板。请切换到目标 Agent 窗口，Ctrl+V 发送，"
                                  "再把对方的回复粘贴到下面，点「回传给 App」：").pack(anchor="w")
        self.reply = tk.Text(self.wait, height=5)
        self.reply.pack(fill="x", pady=4)
        ttk.Button(self.wait, text="回传给 App", command=self.deliver).pack(anchor="e")

        self.log = scrolledtext.ScrolledText(f, height=12, state="disabled")
        self.log.pack(fill="both", expand=True)
        self._refresh_bindings()

    def _refresh_bindings(self):
        names = [MOCK_LABEL] + sorted(self.settings["profiles"].keys())
        for combo, role in ((self.combo_a, "A"), (self.combo_b, "B")):
            combo["values"] = names
            bound = self.settings["bind"].get(role, "")
            combo.set(bound if bound in names else (names[0] if names else ""))
        # 同步设置页的绑定显示
        if hasattr(self, "bind_label_a"):
            self.bind_label_a.config(text=self.combo_a.get() or "未绑定")
            self.bind_label_b.config(text=self.combo_b.get() or "未绑定")

    # ==================== 时间线页 ====================
    def _build_timeline(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="轮次时间线")

        left = ttk.Frame(f)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="会话").pack(anchor="w")
        self.sess_list = tk.Listbox(left, width=44, exportselection=False)
        self.sess_list.pack(fill="y", expand=True)
        self.sess_list.bind("<<ListboxSelect>>", self._on_session)
        ttk.Button(left, text="刷新", command=self.refresh_sessions).pack(fill="x", pady=4)

        right = ttk.Frame(f)
        right.pack(side="left", fill="both", expand=True, padx=8)
        ttk.Label(right, text="轮次（点选查看详情）").pack(anchor="w")
        self.round_list = tk.Listbox(right, height=8, exportselection=False)
        self.round_list.pack(fill="x")
        self.round_list.bind("<<ListboxSelect>>", self._on_round)
        self.round_detail = scrolledtext.ScrolledText(right, state="disabled")
        self.round_detail.pack(fill="both", expand=True, pady=4)
        ttk.Button(right, text="导出会话 JSON（脱敏后）",
                   command=self.export_json).pack(anchor="e")
        self.store = Store()

    def refresh_sessions(self):
        self.sess_list.delete(0, "end")
        self._sessions = self.store.sessions_list()
        for s in self._sessions:
            self.sess_list.insert("end",
                                  f"#{s['id']} [{s['status'] or '运行中'}] {s['directive'][:28]}")

    def _on_session(self, _):
        sel = self.sess_list.curselection()
        if not sel:
            return
        s = self._sessions[sel[0]]
        self._cur_sid = s["id"]
        self.round_list.delete(0, "end")
        self._rounds = self.store.rounds(self._cur_sid)
        for r in self._rounds:
            self.round_list.insert("end",
                                   f"轮 {r['round_no']} [{r['status']}@{r['stage']}] {r['duration_ms']}ms")

    def _on_round(self, _):
        sel = self.round_list.curselection()
        if not sel or not hasattr(self, "_rounds"):
            return
        r = self._rounds[sel[0]]
        self.round_detail.configure(state="normal")
        self.round_detail.delete("1.0", "end")
        self.round_detail.insert("end", "\n".join([
            f"=== 轮 {r['round_no']} [{r['status']}@{r['stage']}] 耗时 {r['duration_ms']}ms ===",
            "\n--- 任务包（A 原文，未脱敏） ---\n" + (r["task_pack"] or "（未产生）"),
            "\n--- 发给 B（脱敏后） ---\n" + (r["sent_to_b"] or "（未产生）"),
            "\n--- B 产出 ---\n" + (r["b_output"] or "（未产生）"),
            "\n--- 验收意见 ---\n" + (r["verdict"] or "（未产生）"),
        ]))
        self.round_detail.configure(state="disabled")

    def export_json(self):
        if not getattr(self, "_cur_sid", None):
            messagebox.showinfo("导出", "请先在左侧选择一个会话")
            return
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            initialfile=f"session_{self._cur_sid}.json")
        if not path:
            return
        self.store.export_session(self._cur_sid, path)
        self.log_line(f"已导出（导出内容已重新脱敏）：{path}")

    # ==================== 设置页 ====================
    def _build_settings_tab(self, nb):
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="设置")

        top = ttk.Frame(f)
        top.pack(fill="both", expand=True)
        left = ttk.Frame(top)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Agent 配置池").pack(anchor="w")
        self.prof_list = tk.Listbox(left, width=26, exportselection=False)
        self.prof_list.pack(fill="y", expand=True)
        self.prof_list.bind("<<ListboxSelect>>", self._on_profile)
        for name in sorted(self.settings["profiles"].keys()):
            self.prof_list.insert("end", name)

        form = ttk.Frame(top)
        form.pack(side="left", fill="both", expand=True, padx=8)
        self.fields = {}
        rows = [("名称", "name", ""), ("类型 (api/clipboard)", "type", "api"),
                ("base_url (API 类)", "base_url", ""), ("api_key (API 类)", "api_key", ""),
                ("model (API 类)", "model", ""), ("目标程序路径 (剪贴板类)", "launch_command", "")]
        for label, key, default in rows:
            r = ttk.Frame(form)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=label, width=24).pack(side="left")
            var = tk.StringVar(value=default)
            entry = ttk.Entry(r, textvariable=var, show="*" if key == "api_key" else "")
            entry.pack(fill="x")
            self.fields[key] = var
        fb = ttk.Frame(form)
        fb.pack(fill="x", pady=6)
        ttk.Button(fb, text="新增", command=self.profile_new).pack(side="left")
        ttk.Button(fb, text="保存", command=self.profile_save).pack(side="left", padx=6)
        ttk.Button(fb, text="删除", command=self.profile_delete).pack(side="left")
        ttk.Button(fb, text="测试连接", command=self.profile_test).pack(side="left", padx=6)

        bindrow = ttk.Frame(f)
        bindrow.pack(fill="x", pady=6)
        ttk.Label(bindrow, text="当前绑定  A:").pack(side="left")
        self.bind_label_a = ttk.Label(bindrow, text="未绑定")
        self.bind_label_a.pack(side="left", padx=(0, 18))
        ttk.Label(bindrow, text="B:").pack(side="left")
        self.bind_label_b = ttk.Label(bindrow, text="未绑定")
        self.bind_label_b.pack(side="left")
        ttk.Label(bindrow, text="（在「任务布置」页下拉切换）").pack(side="left", padx=8)
        dflt = ttk.Frame(f)
        dflt.pack(fill="x")
        ttk.Label(dflt, text="默认参数已随「任务布置」页的轮数/超时在启动会话时保存").pack(side="left")

    def _on_profile(self, _):
        sel = self.prof_list.curselection()
        if not sel:
            return
        name = self.prof_list.get(sel[0])
        prof = self.settings["profiles"].get(name, {})
        self.fields["name"].set(name)
        for key in ("type", "base_url", "api_key", "model", "launch_command"):
            self.fields[key].set(prof.get(key, ""))

    def profile_new(self):
        for key, val in (("name", "新配置"), ("type", "api"), ("base_url", ""),
                         ("api_key", ""), ("model", ""), ("launch_command", "")):
            self.fields[key].set(val)

    def profile_save(self):
        name = self.fields["name"].get().strip()
        if not name or name == MOCK_LABEL:
            messagebox.showwarning("保存", "名称不能为空且不能与内建 Mock 重名")
            return
        prof = {k: self.fields[k].get().strip() for k in
                ("type", "base_url", "api_key", "model", "launch_command")}
        if prof["type"] == "api" and not all((prof["base_url"], prof["api_key"], prof["model"])):
            messagebox.showwarning("保存", "API 类需要 base_url / api_key / model 全部填写")
            return
        self.settings["profiles"][name] = prof
        settings_mod.save(self.settings)
        self.prof_list.delete(0, "end")
        for n in sorted(self.settings["profiles"]):
            self.prof_list.insert("end", n)
        self._refresh_bindings()
        self.log_line(f"已保存 Agent 配置：{name}")

    def profile_delete(self):
        sel = self.prof_list.curselection()
        if not sel:
            return
        name = self.prof_list.get(sel[0])
        self.settings["profiles"].pop(name, None)
        for role in ("A", "B"):
            if self.settings["bind"][role] == name:
                self.settings["bind"][role] = ""
        settings_mod.save(self.settings)
        self.prof_list.delete(sel[0])
        self._refresh_bindings()

    def profile_test(self):
        prof = {k: self.fields[k].get().strip() for k in ("type", "base_url", "api_key", "model")}
        if prof["type"] != "api":
            messagebox.showinfo("测试连接", "剪贴板类无需连接测试")
            return
        if not all((prof["base_url"], prof["api_key"], prof["model"])):
            messagebox.showwarning("测试连接", "请先填 base_url / api_key / model")
            return

        def run():
            try:
                reply = ApiAdapter(prof["base_url"], prof["api_key"], prof["model"]).test_connection()
                self.root.after(0, lambda: messagebox.showinfo("测试连接", f"可用 ✓\n{reply[:120]}"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("测试连接", f"不可用 ✗\n{e}"))
        threading.Thread(target=run, daemon=True).start()

    # ==================== 剪贴板回传（F1/F6 路由） ====================
    def put_clipboard(self, text):
        def _do():
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        self.root.after(0, _do)
        self.logq.put("已把内容写入系统剪贴板（等待人工粘贴 + 回传）")
        self.root.after(0, lambda: self.wait.pack(fill="x", pady=4))

    def notify_wait(self, adapter, payload, token):
        self._waiting = (adapter, token)
        self.logq.put(f"剪贴板模式：等待用户操作（本轮发出 {len(payload)} 字符）…")

    def deliver(self):
        adapter, token = self._waiting or (None, -1)
        text = self.reply.get("1.0", "end").strip()
        if not adapter or not adapter.deliver(token, text):
            self.log_line("回传已丢弃：该次等待已超时或已结束（须在下一轮等待期间重新回传）")
            return
        self.root.after(0, self.wait.pack_forget)

    # ==================== 会话控制 ====================
    def build_for_role(self, role, combo):
        name = combo.get()
        self.settings["bind"][role] = name
        if name == MOCK_LABEL:
            if not self.mock:
                self.mock = start_mock()
            url = self.mock[0] if role == "A" else self.mock[1]
            return ApiAdapter(url, "demo-key-not-real", "mock")
        prof = self.settings["profiles"].get(name)
        if not prof:
            raise ValueError(f"未找到绑定的 Agent 配置: {name}（请在设置页添加）")
        if prof.get("type") == "clipboard":
            return ClipboardAdapter(self.put_clipboard, self.notify_wait,
                                    prof.get("launch_command", ""))
        if not all((prof.get("base_url"), prof.get("api_key"), prof.get("model"))):
            raise ValueError(f"配置 {name} 缺少 base_url / api_key / model")
        return ApiAdapter(prof["base_url"], prof["api_key"], prof["model"])

    def start(self):
        try:
            a, b = self.build_for_role("A", self.combo_a), self.build_for_role("B", self.combo_b)
        except ValueError as e:
            self.log_line(f"配置错误：{e}")
            return
        directive = self.directive.get("1.0", "end").strip()
        if not directive:
            self.log_line("请先填写用户指令")
            return
        self.settings["max_rounds"] = int(self.max_rounds.get())
        self.settings["round_timeout"] = int(self.timeout.get())
        settings_mod.save(self.settings)
        self.clip_adapters = [x for x in (a, b) if isinstance(x, ClipboardAdapter)]
        cfg = {"max_rounds": self.settings["max_rounds"],
               "round_timeout": self.settings["round_timeout"]}
        self.engine = Engine(a, b, self.store, cfg)

        def run():
            try:
                self.engine.run(directive, log=lambda m: self.logq.put(m))
            finally:
                self.logq.put("—— 会话线程结束 ——")
                self.root.after(0, self._session_done)

        threading.Thread(target=run, daemon=True).start()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_replan.configure(state="normal")

    def _session_done(self):
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.btn_replan.configure(state="disabled")
        self.refresh_sessions()

    def stop(self):
        if self.engine:
            self.engine.stop()
        for adapter in self.clip_adapters:
            adapter.abort()
        self.log_line("已请求终止（当前轮结束后生效；剪贴板等待立即解除）")

    def replan(self):
        if not self.engine:
            return
        self.engine.request_replan()
        self.log_line("已请求终止本轮并重新布置（当前阶段结束后作废本轮，A 将重新出任务包）")

    # ==================== 日志 ====================
    def log_line(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self):
        while not self.logq.empty():
            self.log_line(self.logq.get())
        self.root.after(100, self._poll)


def run_demo():
    a_url, b_url, stop = start_mock()
    print(f"[demo] 本地 mock Agent 已启动: A={a_url}  B={b_url}")
    logs = []
    engine = Engine(ApiAdapter(a_url, "demo-key-not-real", "mock-a"),
                    ApiAdapter(b_url, "demo-key-not-real", "mock-b"),
                    Store(), {"max_rounds": 5, "round_timeout": 60})
    result = engine.run("修复 demo_project 的 bug #12：用户登录后头像不刷新", log=logs.append)
    for line in logs:
        print("  " + line)
    print(f"\n[demo] 会话结果: {result['status']}，共 {result['rounds']} 轮")
    print(f"[demo] 最终产出:\n{result['final_output']}")
    print("\n[demo] SQLite 留痕（每轮任务包/产出/验收/耗时）:")
    for r in engine.store.rounds(result["session_id"]):
        print(f"  轮 {r['round_no']} [{r['status']}@{r['stage']}] {r['duration_ms']}ms | "
              f"脱敏后发出 {len(r['sent_to_b'] or '')} 字符 | B 产出 {len(r['b_output'] or '')} 字符 | "
              f"验收: {(r['verdict'] or '-')[:60]}")
    stop()


def run_e2e(directive):
    """无界面真实端到端：A/B 取 data/settings.json 的绑定。"""
    cfg = settings_mod.load()
    pool = cfg["profiles"]
    cli = ClipboardAdapter(lambda t: print(f"\n[e2e-剪贴板] 已写入剪贴板 {len(t)} 字符，请粘贴到目标 Agent，"
                                           f"回传请用 UI；CLI 模式仅建议 API 类"),
                           lambda *a: None)

    def build(role):
        name = cfg["bind"].get(role, "")
        if name == MOCK_LABEL:
            raise ValueError("e2e 请绑定真实 API 配置，不要用 Mock")
        prof = pool.get(name)
        if not prof:
            raise ValueError(f"角色 {role} 未绑定有效配置（settings.json bind.{role}）")
        if prof.get("type") == "clipboard":
            return cli
        return ApiAdapter(prof["base_url"], prof["api_key"], prof["model"])

    engine = Engine(build("A"), build("B"), Store(),
                    {"max_rounds": cfg["max_rounds"], "round_timeout": cfg["round_timeout"]})
    result = engine.run(directive, log=print)
    print(f"\n[e2e] 结果: {result['status']}，共 {result['rounds']} 轮，会话 #{result['session_id']}")
    if result["final_output"]:
        print(f"[e2e] 最终产出:\n{result['final_output']}")


def ui_selftest(root, app):
    """UI 自检：自动触发一次 mock 会话，日志出现 success 即通过。"""
    app.directive.insert("1.0", "UI 自检：修复 demo_project 的 bug #12：用户登录后头像不刷新")
    app.start()
    def check(n=0):
        text = app.log.get("1.0", "end")
        if "success" in text or "failed" in text:
            ok = "success" in text
            print(("UI-SELFTEST OK" if ok else "UI-SELFTEST FAIL") + " | " + text.replace("\n", " / "))
            root.destroy()
            sys.exit(0 if ok else 1)
        if n > 300:
            print("UI-SELFTEST FAIL: 超时未见会话结束"); root.destroy(); sys.exit(1)
        root.after(200, lambda: check(n + 1))
    root.after(200, check)


if __name__ == "__main__":
    if "--demo" in sys.argv:
        run_demo()
    elif "--e2e" in sys.argv:
        i = sys.argv.index("--e2e")
        run_e2e(sys.argv[i + 1] if len(sys.argv) > i + 1 else "（空指令）")
    else:
        root = tk.Tk()
        app = App(root)
        app.refresh_sessions()  # 时间线页预载历史会话
        if "--ui-shot" in sys.argv:  # 截图模式：切到指定 tab 后驻留
            root.after(400, lambda: app.nb.select(int(sys.argv[sys.argv.index("--ui-shot") + 1])))
        if "--ui-selftest" in sys.argv:
            root.after(800, lambda: ui_selftest(root, app))
        root.mainloop()
