"""端到端验证场景（真实闭环）：B 是真实读写文件的 CLI Agent（agent_b_fix.py），
验收基于引擎真实执行任务包 verify 命令所得证据，而非 B 自述。

流程：demo_project 复制到临时目录（夹具不动）；B 在临时目录里真改 calc.py；
A 有真实 API 绑定（bind.A -> type=api）则用真实 LLM，否则用 ScriptedA（解析
review prompt 中的【执行验证结果】判定，退出码全 0 才通过）。引擎跑完后独立断言
bug 真被修好（py test_calc.py 退出码 0、防护行已写入），最后清理临时目录。

用法：py scenarios/e2e_exec.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adapters import ApiAdapter, CliAdapter
from engine import Engine, Store
import settings as settings_mod

HERE = os.path.dirname(os.path.abspath(__file__))


class ScriptedA:
    """脚本 A：真实解析 review prompt 中的【执行验证结果】证据段——所有退出码为 0
    才判通过；任何非 0/超时按硬规则判不通过并引用失败原因。不无条件通过。"""
    label = "scripted-a"

    def __init__(self):
        self.prompts = []
        self.verdicts = []

    def send(self, payload, timeout):
        self.prompts.append(payload)
        if payload.startswith("你是任务布置型 Agent A，现在验收"):
            codes = re.findall(r"退出码: (\S+)", payload)
            if "【执行验证结果" not in payload or not codes:
                v = {"passed": False, "comments": "验收缺少执行验证证据，不得判通过"}
            elif all(c == "0" for c in codes):
                v = {"passed": True,
                     "comments": f"执行验证 {len(codes)} 条命令退出码全 0，bug 已真实修复，验收通过"}
            else:
                v = {"passed": False,
                     "comments": "执行验证失败（退出码 " + "/".join(codes) + "），按硬规则不得判通过"}
            self.verdicts.append(v)
            return json.dumps(v, ensure_ascii=False)
        pack = {"goal": "修复 calc.py discount() 对负数数量返回负总价的 bug",
                "inputs": ["calc.py", "test_calc.py"],
                "steps": ["在 discount 函数体开头插入 qty <= 0 返回 0 的防护"],
                "acceptance": ["py test_calc.py 全部断言通过（退出码 0）"],
                "forbidden": ["不得修改 test_calc.py"],
                "verify": ["py test_calc.py"]}
        return json.dumps(pack, ensure_ascii=False)


def main():
    cfg = settings_mod.load()
    prof = cfg["profiles"].get(cfg["bind"].get("A", ""))
    tmp = tempfile.mkdtemp(prefix="relay_e2e_exec_")
    try:
        shutil.copytree(os.path.join(HERE, "demo_project"), tmp, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
        if prof and prof.get("type") == "api":
            a = ApiAdapter(prof["base_url"], prof["api_key"], prof["model"])
            print(f"[e2e-exec] A 使用真实 LLM（bind.A -> {cfg['bind'].get('A')}）")
        else:
            a = ScriptedA()
            print("[e2e-exec] bind.A 无可用 api 配置，A 使用 ScriptedA（解析执行验证证据判定）")
        b = CliAdapter(f'py "{os.path.join(HERE, "agent_b_fix.py")}"', cwd=tmp)
        engine = Engine(a, b, Store(),
                        {"max_rounds": 3, "round_timeout": 120, "verify_cwd": tmp})
        directive = ("修复临时目录中 calc.py 的负数计价 bug：discount(-3) 与 discount(0) 必须返回 0，"
                     "其余行为不变。验收以真实执行为准，任务包必须带 verify 字段：[\"py test_calc.py\"]。")
        result = engine.run(directive, log=print)
        # 独立断言：不信任引擎与 B 的任何自述，直接在临时目录跑测试
        t = subprocess.run("py test_calc.py", shell=True, capture_output=True,
                           cwd=tmp, encoding="utf-8", errors="replace")
        calc = open(os.path.join(tmp, "calc.py"), encoding="utf-8").read()
        assert t.returncode == 0, f"独立断言失败：test_calc.py 退出码 {t.returncode}\n{t.stdout}{t.stderr}"
        assert "if qty <= 0:" in calc and "return 0" in calc, "防护行未写入 calc.py"
        print("[e2e-exec] 独立断言通过：test_calc.py 退出码 0，防护行已写入 calc.py")
        print(f"[e2e-exec] 会话结果: {result['status']}，共 {result['rounds']} 轮，"
              f"会话 #{result['session_id']}")
        ok = result["status"] == "success"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
