"""命令行入口（正式 UI 已迁移至 web 版：py server.py）。

用法：
  py app.py --demo             无界面端到端演示（本地 mock，3 轮循环验收通过）
  py app.py --e2e "指令"       无界面真实端到端：按 data/settings.json 的 A/B 绑定跑
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")

from adapters import ApiAdapter, ClipboardAdapter
from engine import Engine, Store
from mock_agent import start_mock
import settings as settings_mod


def run_demo():
    a_url, b_url, stop = start_mock()
    print(f"[demo] 本地 mock Agent 已启动: A={a_url}  B={b_url}")
    engine = Engine(ApiAdapter(a_url, "demo-key-not-real", "mock-a"),
                    ApiAdapter(b_url, "demo-key-not-real", "mock-b"),
                    Store(), {"max_rounds": 5, "round_timeout": 60})
    result = engine.run("修复 demo_project 的 bug #12：用户登录后头像不刷新", log=print)
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
    cli = ClipboardAdapter(lambda t: print(f"\n[e2e-剪贴板] 已写入剪贴板 {len(t)} 字符；CLI 模式回传请用桌面版"),
                           lambda *a: None)

    def build(role):
        name = cfg["bind"].get(role, "")
        if name == "本地Mock(测试用)":
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


if __name__ == "__main__":
    if "--demo" in sys.argv:
        run_demo()
    elif "--e2e" in sys.argv:
        i = sys.argv.index("--e2e")
        run_e2e(sys.argv[i + 1] if len(sys.argv) > i + 1 else "（空指令）")
    else:
        print(__doc__)
