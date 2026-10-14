"""P1 自检：py test_p1.py。覆盖 F17-F21 与 P1 新功能：重新布置/导出脱敏/设置池/聚焦冒烟。"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")

from adapters import Aborted, AdapterError, CliAdapter, ClipboardAdapter, focus_window_by_exe
from engine import Engine, ReplanRequested, Store, sanitize_obj
import settings as settings_mod


class FakeA:
    label = "fake-a"

    def __init__(self, verdicts=None):
        self.verdicts = verdicts or [(True, "通过")]
        self.n_review = 0
        self.prompts = []

    def send(self, payload, timeout):
        self.prompts.append(payload)
        if payload.startswith("你是任务布置型 Agent A，现在验收"):
            i = min(self.n_review, len(self.verdicts) - 1)
            self.n_review += 1
            passed, c = self.verdicts[i]
            return f'{{"passed": {str(passed).lower()}, "comments": "{c}"}}'
        pack = {"goal": "修 bug", "inputs": ["报告"], "steps": ["定位"],
                "acceptance": ["测试过"], "forbidden": ["不改无关文件"]}
        import json as _json
        return _json.dumps(pack, ensure_ascii=False)


class FakeB:
    label = "fake-b"
    hooks = []            # (engine, 轮次) 回调钩子，测试用

    def send(self, payload, timeout):
        json.loads(payload)
        return "B 的产出：修复完成"


def fresh_engine(a, b, cfg=None):
    logs = []
    store = Store(":memory:")
    eng = Engine(a, b, store, cfg or {"max_rounds": 5, "round_timeout": 30})
    return eng, store, logs


# 1. F20：B 执行中收到「终止本轮重新布置」→ 该轮作废留痕，下一轮 A 收到重新布置提示
class ReplanB(FakeB):
    triggered = False

    def send(self, payload, timeout):
        out = super().send(payload, timeout)
        if not ReplanB.triggered:
            ReplanB.triggered = True
            ENGINE_REF[0].request_replan()   # 模拟用户首次 B 执行期间点击
        return out


ENGINE_REF = [None]
a = FakeA(verdicts=[(False, "第一轮意见：不完整"), (True, "第二轮通过")])
eng, store, logs = fresh_engine(a, ReplanB())
ENGINE_REF[0] = eng
r = eng.run("修 bug", log=logs.append)
assert r["status"] == "success" and r["rounds"] >= 2, r
rows = store.rounds(r["session_id"])
assert any(row[1] == "replan" for row in rows), [row[1] for row in rows]
assert any("重新布置" in p and "用户终止" in p for p in a.prompts), "下一轮未见重新布置提示"
replan_row = [row for row in rows if row[1] == "replan"][0]
assert replan_row[3] and replan_row[4] and replan_row[5], "replan 轮未保留已产生数据"
ENGINE_REF[0] = None

# 2. F20：终检——用户不能改任务包内容，只能要求重新出（引擎无任何改包入口，此为结构断言）
import engine as engine_mod
src = open(engine_mod.__file__, encoding="utf-8").read()
assert "def amend" not in src and "modify_pack" not in src

# 3. F21：导出 JSON——task_pack 重新脱敏、文本字段过文本脱敏、结构完整
store = Store(":memory:")
eng = Engine(FakeA(), FakeB(), store, {"max_rounds": 1, "round_timeout": 30})
r = eng.run("修 bug", log=lambda m: None)
# 手动注入含敏感值的原文留痕，验证导出脱敏
store._db.execute("UPDATE rounds SET task_pack=? WHERE session_id=?",
                  (json.dumps({"goal": "g", "inputs": ["sk-e2e1234567890abcd"]}), r["session_id"]))
store._db.execute("UPDATE rounds SET b_output=? WHERE session_id=?",
                  ("产出包含 13812345678 手机号", r["session_id"]))
store._db.commit()
# 导出到临时目录下不存在的子目录：既不依赖工作树 data/（A1），也回归导出自动建父目录（A3）
export_dir = tempfile.mkdtemp()
path = store.export_session(r["session_id"], os.path.join(export_dir, "no_such_dir_xyz", "test_export.json"))
exported = json.load(open(path, encoding="utf-8"))
assert "sk-e2e1234567890abcd" not in json.dumps(exported, ensure_ascii=False), "F21：敏感 key 原样导出"
assert "13812345678" not in json.dumps(exported, ensure_ascii=False), "手机号原样导出"
assert "[已脱敏:" in json.dumps(exported, ensure_ascii=False), "F21：导出未见脱敏占位"
assert exported["session"]["id"] == r["session_id"] and exported["rounds"]
assert json.loads(exported["rounds"][0]["task_pack"])["goal"] == "g"  # 仍是合法 JSON
shutil.rmtree(export_dir)

# 4. sessions_list 倒序可查
lst = store.sessions_list()
assert lst and lst[0]["id"] == r["session_id"]

# 5. 设置池读写往返 + 缺省合并（临时目录，不碰工作树 data/）
tmp_dir = tempfile.mkdtemp()
tmp = os.path.join(tmp_dir, "test_settings.json")
settings_mod.save({"profiles": {"我的GLM": {"type": "api", "base_url": "https://x/v1",
                                             "api_key": "sk-x", "model": "m"}},
                   "bind": {"A": "我的GLM", "B": ""}, "max_rounds": 7, "round_timeout": 300}, tmp)
cfg = settings_mod.load(tmp)
assert cfg["profiles"]["我的GLM"]["model"] == "m" and cfg["bind"]["A"] == "我的GLM"
assert cfg["max_rounds"] == 7
empty = settings_mod.load(os.path.join(tmp_dir, "definitely_missing.json"))
assert empty == settings_mod.DEFAULTS or empty["max_rounds"] == 5  # 缺文件回默认
shutil.rmtree(tmp_dir)

# 6. F17 冒烟：不存在的进程聚焦返回 False（真实置前由 UI 手测）
assert focus_window_by_exe("definitely_not_running_xyz.exe") is False

# 7. replan 在 A 出包阶段触发（_get_json / 轮循环检查点均生效）
class PlanReplanA(FakeA):
    def __init__(self, verdicts=None):
        super().__init__(verdicts)
        self.triggered = False

    def send(self, payload, timeout):
        if not self.triggered and not payload.startswith("你是任务布置型 Agent A，现在验收"):
            self.triggered = True
            ENGINE_REF[0].request_replan()  # 首次出包时请求重新布置
        return super().send(payload, timeout)


a7 = PlanReplanA(verdicts=[(True, "通过")])
eng, store, logs = fresh_engine(a7, FakeB(), {"max_rounds": 2, "round_timeout": 30})
ENGINE_REF[0] = eng
r = eng.run("修 bug", log=logs.append)
assert r["status"] == "success" and r["rounds"] == 2, r
rows = store.rounds(r["session_id"])
assert rows[0][1] == "replan" and rows[1][1] == "ok", [row[1] for row in rows]
ENGINE_REF[0] = None

# 8. CliAdapter：本地命令行 Agent——stdin/stdout 管道、超时 kill、错误归一
UPPER = CliAdapter('py -c "import sys; print(\'B产出:\' + sys.stdin.read().strip().upper())"')
assert UPPER.send("hello 任务包", 30.0) == "B产出:HELLO 任务包"

try:  # 超时 → TimeoutError 且进程被 kill
    CliAdapter('py -c "import time; time.sleep(5)"').send("x", 1.0)
    assert False, "应超时"
except TimeoutError:
    pass

try:  # 非零退出码 → AdapterError 带 stderr 尾部
    CliAdapter('py -c "import sys; sys.stderr.write(\'agent 崩溃信息\'); sys.exit(2)"').send("x", 30.0)
    assert False, "应抛 AdapterError"
except AdapterError as e:
    assert "退出码 2" in str(e) and "崩溃信息" in str(e)

try:  # 空 stdout → AdapterError
    CliAdapter("py -c pass").send("x", 30.0)
    assert False, "空输出应抛 AdapterError"
except AdapterError:
    pass

try:  # 命令不存在 → AdapterError（不裸抛 FileNotFoundError）
    CliAdapter("definitely_not_exist_xyz.exe --flag").send("x", 5.0)
    assert False
except AdapterError:
    pass

# 引擎集成：B 为 CliAdapter（py 模拟本地 agent），完整跑一轮 success
eng, store, logs = fresh_engine(FakeA(verdicts=[(True, "通过")]), UPPER,
                                {"max_rounds": 1, "round_timeout": 60})
r = eng.run("修 bug", log=logs.append)
assert r["status"] == "success" and "B产出:" in r["final_output"], r

print("OK — P1 全部自检通过")
