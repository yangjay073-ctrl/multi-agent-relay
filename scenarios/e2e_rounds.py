"""端到端验证场景：修复项目 bug，A 为真实 LLM（读 data/settings.json 的 A 绑定），
B 为「渐进补全」执行代理——模拟一个不完美的执行 Agent：第 1 轮只交付部分产出，
按 A 的打回意见逐轮补齐。用于真实验证多轮循环（打回→补全→再验收）。

用法：py scenarios/e2e_rounds.py
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adapters import ApiAdapter
from engine import Engine, Store
import settings as settings_mod

ITEMS = {
    "1": "【第1项·修复后的 discount 完整代码】\n```python\ndef discount(qty: int) -> int:\n    \"\"\"批发价：单价 100 元；满 10 件整单 8 折；qty 为负或 0 时返回 0。\"\"\"\n    if qty <= 0:\n        return 0\n    if qty >= 10:\n        return int(qty * 100 * 0.8)\n    return qty * 100\n```",
    "2": "【第2项·边界自测表】\n| 输入 qty | 预期值 | 实际值 | 结果 |\n|---|---|---|---|\n| -3 | 0 | 0 | 通过 |\n| 0 | 0 | 0 | 通过 |\n| 1 | 100 | 100 | 通过 |\n| 9 | 900 | 900 | 通过 |\n| 10 | 800 | 800 | 通过 |\n| 20 | 1600 | 1600 | 通过 |",
    "3": "【第3项·一行变更说明】函数入口新增 qty <= 0 返回 0 的前置防护，原 1..9 全价与 ≥10 八折逻辑不变。",
    "4": "【第4项·回归影响分析】discount(1)=100、discount(9)=900 不经过新分支，结果不变；discount(10)=800、discount(20)=1600 的八折分支未改动，结果不变；新增分支仅影响 qty<=0 的输入。",
    "5": "【第5项·新版函数 docstring 原文】\"\"\"批发价：单价 100 元；满 10 件整单 8 折；qty 为负或 0 时返回 0。\"\"\" ——已包含对负数与 0 行为的说明。",
}


class ProgressiveB:
    """渐进补全的执行代理：第 1/2/3 轮分别交付 3/4/5 项（模拟不完美的执行 Agent）。"""
    label = "progressive-b"

    def send(self, payload, timeout):
        pack = json.loads(payload)
        round_no = pack.get("meta", {}).get("round", 1)
        upto = {1: 2, 2: 4, 3: 5}.get(round_no, 5)   # 轮1->2项, 轮2->4项, 轮3->全5项
        return "\n\n".join(ITEMS[k] for k in sorted(ITEMS)[:upto])


def main():
    cfg = settings_mod.load()
    prof = cfg["profiles"].get(cfg["bind"].get("A", ""))
    if not prof or prof.get("type") != "api":
        sys.exit("请先在 data/settings.json 为角色 A 绑定真实 API 配置")
    a = ApiAdapter(prof["base_url"], prof["api_key"], prof["model"])
    engine = Engine(a, ProgressiveB(), Store(),
                    {"max_rounds": cfg["max_rounds"], "round_timeout": cfg["round_timeout"]})
    directive = (
        "修复 scenarios/demo_project 的 bug 并交付完整变更包。背景：批发计价 calc.py 的 "
        "discount(qty) 对负数数量返回负总价（discount(-3)=-300），正确行为是负数与 0 都返回 0。\n"
        "【calc.py 现状】\ndef discount(qty):\n    if qty >= 10:\n        return int(qty * 100 * 0.8)\n"
        "    return qty * 100\n"
        "【测试断言（不得修改）】assert calc.discount(-3) == 0\n"
        "【B 的产出必须同时包含以下五项，编号给出】\n"
        "1) 修复后的 discount 完整代码；\n"
        "2) 边界自测表（-3/0/1/9/10/20 六输入 × 预期值/实际值/结果三列）；\n"
        "3) 一行变更说明；\n"
        "4) 回归影响分析：逐条说明改动对既有断言 discount(1)/discount(9)/discount(10)/discount(20) 的影响；\n"
        "5) 新版函数的 docstring 原文（须包含对负数与 0 行为的说明）。\n"
        "【A 验收纪律】五项逐项核对，编号引用；任何一项缺失、内容与要求不符或编号错乱，即判不通过"
        "并在意见中明确指出缺哪几项；全部齐备才通过。"
    )
    result = engine.run(directive, log=print)
    print(f"\n[e2e-rounds] 结果: {result['status']}，共 {result['rounds']} 轮，会话 #{result['session_id']}")
    sys.exit(0 if result["status"] == "success" else 1)


if __name__ == "__main__":
    main()
