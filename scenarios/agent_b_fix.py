"""真实执行的执行型 Agent B（CLI 夹具）：从 stdin 读任务包 JSON，在 cwd 下 calc.py 的
discount 函数体开头插入负数防护（真实读写文件，非只输出文本），stdout 输出简短变更说明。
经 CliAdapter 调用：py scenarios/agent_b_fix.py（cwd 指向 demo_project 的临时副本）。"""
import json
import sys

sys.stdin.reconfigure(encoding="utf-8", errors="replace")   # 与 CliAdapter 的 utf-8 管道配套
sys.stdout.reconfigure(encoding="utf-8")

GUARD = "    if qty <= 0:\n        return 0\n"
ANCHOR = "    if qty >= 10:"


def main():
    try:
        pack = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        pack = {}
    src = open("calc.py", encoding="utf-8").read()
    if "if qty <= 0:" in src:
        print("calc.py 已含负数防护，本轮无代码变更。")
        return
    if ANCHOR not in src:
        sys.stderr.write("calc.py 中未找到 discount 的插入锚点\n")
        sys.exit(1)
    with open("calc.py", "w", encoding="utf-8") as f:
        f.write(src.replace(ANCHOR, GUARD + ANCHOR, 1))
    print(f"已修复 calc.py：在 discount 入口插入 qty<=0 返回 0 的防护"
          f"（任务目标：{pack.get('goal', '-')[:60]}）。变更 1 个文件，+2 行。")


if __name__ == "__main__":
    main()
