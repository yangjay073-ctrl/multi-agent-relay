"""P2 自检：py test_p2.py。覆盖任务包 verify 校验、verify 真实执行与验收证据注入、
反例（B 谎称修好但执行失败 → A 依证据不得判通过）、无 verify 回归、旧库迁移、agent_b_fix 夹具。"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

from adapters import CliAdapter
from engine import Engine, Store, _clip, validate_pack

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_B_FIX = os.path.join(HERE, "scenarios", "agent_b_fix.py")
DEMO = os.path.join(HERE, "scenarios", "demo_project")


class FakeB:
    """口径模拟「只回报不干活」的 B：声称已修复、测试全过。"""
    label = "fake-b"

    def send(self, payload, timeout):
        json.loads(payload)
        return "B 的产出：已修复该 bug，py test_calc.py 全部通过，无回归。"


class EvidenceA:
    """出（可选带 verify 的）任务包；review 时真实解析【执行验证结果】证据段判定：
    退出码全 0 才通过，任何非 0/超时判不通过并引用失败原因；无证据段（包无 verify）
    则按 B 自述文本验收（通过）——用于对照回归。"""
    label = "evidence-a"

    def __init__(self, verify=None):
        self.verify = verify
        self.prompts = []
        self.verdicts = []

    def send(self, payload, timeout):
        self.prompts.append(payload)
        if payload.startswith("你是任务布置型 Agent A，现在验收"):
            codes = re.findall(r"退出码: (\S+)", payload)
            if "【执行验证结果" not in payload or not codes:
                v = {"passed": True, "comments": "本包无 verify，按 B 自述与验收标准核对通过"}
            elif all(c == "0" for c in codes):
                v = {"passed": True, "comments": f"执行验证 {len(codes)} 条命令退出码全 0，通过"}
            else:
                v = {"passed": False,
                     "comments": "执行验证失败：退出码 " + "/".join(codes) + "，不得判通过"}
            self.verdicts.append(v)
            return json.dumps(v, ensure_ascii=False)
        pack = {"goal": "修 bug", "inputs": ["报告"], "steps": ["定位", "修复"],
                "acceptance": ["测试过"], "forbidden": ["不改无关文件"]}
        if self.verify is not None:
            pack["verify"] = self.verify
        return json.dumps(pack, ensure_ascii=False)


def run_engine(a, b=None, cfg=None):
    logs = []
    store = Store(":memory:")
    r = Engine(a, b or FakeB(), store,
               cfg or {"max_rounds": 3, "round_timeout": 60}).run("修 bug", log=logs.append)
    return r, logs, store


def review_prompt(a):
    return [p for p in a.prompts if p.startswith("你是任务布置型 Agent A，现在验收")][0]


# 1. validate_pack 的 verify 校验：缺省可选；合法数组过；非法形态报错
full = {"goal": "g", "inputs": ["i"], "steps": ["s"], "acceptance": ["a"], "forbidden": ["f"]}
assert validate_pack(full) == [], "verify 缺省必须通过（可选字段）"
assert validate_pack(dict(full, verify=["py test_calc.py"])) == []
assert validate_pack(dict(full, verify=["a", "b && c"])) == []
assert validate_pack(dict(full, verify="py test_calc.py")) != [], "verify 非数组须报错"
assert validate_pack(dict(full, verify=[])) != [], "verify 空数组视为无效"
assert validate_pack(dict(full, verify=["py t.py", 123])) != [], "元素非字符串须报错"
assert validate_pack(dict(full, verify=["   "])) != [], "空白命令须报错"
clipped = _clip("x" * 5000)
assert len(clipped) < 2100 and "已截断" in clipped and _clip("短文本") == "短文本"

# 2. verify 真实执行：证据注入 review prompt，verify_result 落库
a = EvidenceA(verify=["py -c \"print('证据输出')\""])
r, logs, store = run_engine(a)
assert r["status"] == "success", r
review = review_prompt(a)
assert "【执行验证结果" in review and "非 B 自述" in review
assert "退出码: 0" in review and "证据输出" in review
assert "不得判通过" in review, "硬规则文本未随证据段注入"
vr = store._db.execute("SELECT verify_result FROM rounds WHERE session_id=? AND status='ok'",
                       (r["session_id"],)).fetchone()[0]
assert vr, "verify_result 未落库"
parsed = json.loads(vr)
assert isinstance(parsed, list) and parsed[0]["exit_code"] == 0
assert parsed[0]["command"].startswith("py -c") and "stdout" in parsed[0] and "stderr" in parsed[0]
assert any("真实执行验证" in m and "全部退出码 0" in m for m in logs)

# 3. 反例（本改造核心）：B 声称「已修复测试全过」，verify 真实执行却失败 → A 不得判通过
a = EvidenceA(verify=["py -c \"import sys; sys.exit(3)\""])
r, logs, store = run_engine(a)
assert r["status"] != "success", f"B 谎报但会话竟 success: {r}"
review = review_prompt(a)
assert "退出码: 3" in review and "不得判通过" in review
assert a.verdicts and all(v["passed"] is False for v in a.verdicts), a.verdicts
assert all("3" in v["comments"] for v in a.verdicts), "验收意见未引用失败退出码"
for vr, st in store._db.execute(
        "SELECT verify_result, status FROM rounds WHERE session_id=?", (r["session_id"],)):
    assert st == "ok" and json.loads(vr)[0]["exit_code"] == 3, (st, vr)

# 3b. 反例变体：verify 直接跑仓库里带 bug 的 demo_project 测试（只读执行，不改夹具）
a = EvidenceA(verify=["py scenarios/demo_project/test_calc.py"])
r, logs, store = run_engine(a, cfg={"max_rounds": 1, "round_timeout": 60})
assert r["status"] != "success" and a.verdicts[0]["passed"] is False
review = review_prompt(a)
assert "退出码: 1" in review and "AssertionError" in review, "demo_project 的真实失败未进入证据"

# 4. 无 verify 的包：全链路行为与改造前一致——review prompt 无证据段、verify_result 为 NULL
a = EvidenceA(verify=None)
r, logs, store = run_engine(a)
assert r["status"] == "success" and r["rounds"] == 1, r
assert r["final_output"].startswith("B 的产出")
review = review_prompt(a)
assert "【执行验证结果" not in review and "不得判通过" not in review
assert review.endswith("B 的产出：已修复该 bug，py test_calc.py 全部通过，无回归。")
vr = store._db.execute("SELECT verify_result FROM rounds WHERE session_id=?",
                       (r["session_id"],)).fetchone()[0]
assert vr is None, "无 verify 的轮次 verify_result 应为 NULL"
assert not any("真实执行验证" in m for m in logs)

# 5. 旧库迁移：缺 verify_result（乃至缺 stage）的旧表 → Store 打开不崩、自动补列、可写可读
for cols_extra in ("stage TEXT", ""):  # 两种旧形态：有 stage 无 verify_result / 两者皆无
    tmpdir = tempfile.mkdtemp()
    db = os.path.join(tmpdir, "old.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE rounds(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER,"
                " round_no INTEGER, task_pack TEXT, sent_to_b TEXT, b_output TEXT, verdict TEXT,"
                f" duration_ms INTEGER, status TEXT, started_at TEXT{', ' + cols_extra if cols_extra else ''})")
    con.commit()
    con.close()
    store = Store(db)  # 打开即迁移
    sid = store.create_session("旧库迁移", "a", "b", {})
    store.log_round(sid, 1, None, None, None, None, 5, "ok", "t0", "done")  # 旧 10 参位置调用兼容
    store.log_round(sid, 2, None, None, None, None, 5, "ok", "t1", "done", "[{\"exit_code\": 0}]")
    got = store._db.execute("SELECT stage, verify_result FROM rounds WHERE session_id=?", (sid,)).fetchall()
    assert got[0][0] == "done" and got[0][1] is None, tuple(got[0])
    assert json.loads(got[1][1])[0]["exit_code"] == 0
    assert len(store.rounds(sid)[0]) == 8, "rounds() 列数变化会破坏既有按位置解包"
    store.close()
    shutil.rmtree(tmpdir)

# 6. agent_b_fix 夹具：真实改临时副本 calc.py，插防护后 test_calc 真过；重复执行幂等
tmp = tempfile.mkdtemp()
shutil.copytree(DEMO, tmp, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
fixer = CliAdapter(f'py "{AGENT_B_FIX}"', cwd=tmp)
pack = json.dumps({"goal": "修负数计价 bug", "inputs": [], "steps": [], "acceptance": [], "forbidden": []})
out1 = fixer.send(pack, 30.0)
assert "防护" in out1, out1
src = open(os.path.join(tmp, "calc.py"), encoding="utf-8").read()
assert "if qty <= 0:" in src and "return 0" in src, "防护行未插入"
assert "已含负数防护" not in out1, "首次执行不应判为已修复"
t = subprocess.run("py test_calc.py", shell=True, capture_output=True, cwd=tmp,
                   env={**os.environ, "PYTHONUTF8": "1"}, encoding="utf-8", errors="replace")
assert t.returncode == 0 and "all tests passed" in t.stdout, (t.returncode, t.stdout, t.stderr)
out2 = fixer.send(pack, 30.0)  # 幂等：已含防护时不重复插入、不报错
assert "已含负数防护" in out2
t2 = subprocess.run("py test_calc.py", shell=True, capture_output=True, cwd=tmp,
                    env={**os.environ, "PYTHONUTF8": "1"}, encoding="utf-8", errors="replace")
assert t2.returncode == 0
shutil.rmtree(tmp)

print("OK — P2 全部自检通过")
