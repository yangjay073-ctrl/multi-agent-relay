"""P0 自检：py test_p0.py，全绿输出 OK。覆盖校验打回/脱敏/相似度/引擎全流程/僵局/轮数/超时/留痕。"""
import json
import sys
sys.stdout.reconfigure(encoding="utf-8")

from engine import (Engine, Store, extract_json, validate_pack, validate_verdict,
                    sanitize, sanitize_obj, similarity)
from adapters import Aborted, ClipboardAdapter


class FakeA:
    """可编排的 A：plan 返回任务包（可先给缺字段的），review 按 verdicts 脚本走。"""
    label = "fake-a"

    def __init__(self, bad_first=True, verdicts=None):
        self.bad_first = bad_first
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
        pack = {"goal": "修 bug", "inputs": ["报告", "token: sk-fake1234567890abcd", {"手机": 13900001111}],
                "steps": ["定位", "修复"],
                "acceptance": ["测试过"], "forbidden": ["不改无关文件"]}
        if self.bad_first and "你上次的输出不合法" not in payload:
            pack.pop("forbidden")  # 首次缺 forbidden，等打回
        import json
        return json.dumps(pack, ensure_ascii=False)


class FakeB:
    label = "fake-b"
    def __init__(self, behavior="ok"):
        self.behavior = behavior
    def send(self, payload, timeout):
        if self.behavior == "timeout":
            raise TimeoutError("模拟单轮超时")
        if self.behavior == "error":
            from adapters import AdapterError
            raise AdapterError("模拟连接失败")
        json.loads(payload)  # F2: 发给 B 的内容必须恒为合法 JSON
        assert "sk-fake1234567890abcd" not in payload and "13900001111" not in payload, "敏感值未脱敏"
        return "B 的产出：修复完成"


_last_store = None

def fresh_store():
    global _last_store
    if _last_store is not None:
        _last_store.close()
    # F16: 内存库——不落盘、无删除/占用问题，只读目录与并行运行都不再炸
    _last_store = Store(":memory:")
    return _last_store


def run_engine(a, b, cfg=None):
    logs = []
    store = fresh_store()
    r = Engine(a, b, store, cfg).run("修复登录头像不刷新", log=logs.append)
    return r, logs, store


# 1. extract_json 容错
assert extract_json('{"a":1}') == {"a": 1}
assert extract_json('```json\n{"a":1}\n```') == {"a": 1}
assert extract_json('好的，如下：\n{"a": [1,2]}\n以上') == {"a": [1, 2]}
try:
    extract_json("没有 JSON"); assert False
except ValueError:
    pass

# 2. 任务包必填字段校验
full = {"goal": "g", "inputs": ["i"], "steps": ["s"], "acceptance": ["a"], "forbidden": ["f"]}
assert validate_pack(full) == []
for f in ("goal", "inputs", "steps", "acceptance", "forbidden"):
    bad = dict(full); bad.pop(f)
    assert f in " ".join(validate_pack(bad)), f"缺 {f} 未报错"
assert validate_pack(dict(full, steps=[])) != []
assert validate_verdict({"passed": True, "comments": "ok"}) == []
assert validate_verdict({"comments": "x"}) != []

# 3. 脱敏
dirty = ("key=sk-abcdefghijklmnop1234 token=ghp_" + "x" * 30 +
         " 密钥:\n-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"
         "手机 13812345678 身份证 11010119900307777X Bearer aaaabbbbccccddddeeee")
clean, hits = sanitize(dirty)
assert hits == 6 and "sk-abc" not in clean and "13812345678" not in clean
assert "11010119900307777X" not in clean and "PRIVATE KEY-----\nabc" not in clean
assert "Bearer aaaabbbbcccc" not in clean and "[已脱敏:" in clean
assert sanitize("普通文本，无敏感信息。")[1] == 0

# 4. 相似度（僵局判据）
assert similarity("同一段话", "同一段话") == 1.0
assert similarity("回归测试覆盖不全，需补连续切换账号用例", "缓存清理不彻底，localStorage 有残留风险") < 0.9
assert similarity("意见：测试没过请补用例", "意见：测试没过请补用例。") > 0.9

# 5. 引擎全流程：3 轮（2 不通过 + 1 通过），任务包首次缺字段被打回补全
a = FakeA(verdicts=[(False, "回归测试覆盖不全，需补连续切换账号用例"),
                    (False, "缓存清理不彻底，localStorage 有残留风险"),
                    (True, "三条验收标准逐条通过，验收通过")])
r, logs, store = run_engine(a, FakeB())
assert r["status"] == "success" and r["rounds"] == 3 and "修复完成" in r["final_output"]
assert any("任务包被打回" in m and "forbidden" in m for m in logs), "缺字段未打回"
assert any("输出不合法" in p and "forbidden" in p for p in a.prompts), "打回信息未回传给 A"
rows = store.rounds(r["session_id"])
assert len(rows) == 3 and all(row[1] == "ok" for row in rows)
assert "passed" in rows[0][6] and rows[0][3] and rows[0][4] and rows[0][5], "留痕字段缺失"

# 6. 僵局：连续两次验收意见几乎相同 → 第 2 轮后终止
same = (False, "B 产出不符合验收标准第一条，登录后头像仍未刷新，请重做")
a = FakeA(bad_first=False, verdicts=[same, same, same, same, same])
r, logs, _ = run_engine(a, FakeB())
assert r["status"] == "deadlock" and r["rounds"] == 2, r
assert any("僵局" in m for m in logs)

# 7. 最大轮数：永不通过且意见各不相同 → 轮数用尽
diff = [(False, f"第 {i} 次不通过，问题点 {i} 需要完全不同的处理方式 {i}") for i in range(5)]
a = FakeA(bad_first=False, verdicts=diff)
r, logs, _ = run_engine(a, FakeB(), {"max_rounds": 3, "round_timeout": 30})
assert r["status"] == "max_rounds" and r["rounds"] == 3

# 8. 单轮超时：B 每轮超时 → 各轮记录 timeout，最终轮数用尽
r, logs, store = run_engine(FakeA(bad_first=False), FakeB("timeout"),
                            {"max_rounds": 2, "round_timeout": 30})
assert r["status"] == "max_rounds" and r["rounds"] == 2
assert all(row[1] == "timeout" for row in store.rounds(r["session_id"]))

# 9. 适配器故障：连接失败 → 会话立即失败终止
r, logs, _ = run_engine(FakeA(bad_first=False), FakeB("error"))
assert r["status"] == "failed" and any("连接失败" in m for m in logs)

# 10. A 持续给不出合法任务包 → 打回 3 次后失败
class HopelessA(FakeA):
    def send(self, payload, timeout):
        return "我拒绝输出 JSON"
r, logs, _ = run_engine(HopelessA(), FakeB())
assert r["status"] == "failed" and any("JSON 解析失败" in m for m in logs)

# 11. F1 回归：A、B 同为剪贴板半自动也能完整跑通（回传路由到正在等待者）
FULL_PACK = {"goal": "修 bug", "inputs": ["报告"], "steps": ["定位", "修复"],
             "acceptance": ["测试过"], "forbidden": ["不改无关文件"]}


def make_clip_pair():
    import json as _json

    def user_as_A(adapter, payload, token):
        if payload.startswith("你是任务布置型 Agent A，现在验收"):
            adapter.deliver(token, '{"passed": true, "comments": "全部标准已满足"}')
        elif "你上次的输出不合法" in payload:
            adapter.deliver(token, _json.dumps(FULL_PACK, ensure_ascii=False))
        else:  # 首次出包故意缺 forbidden，顺带走一次打回
            bad = {k: v for k, v in FULL_PACK.items() if k != "forbidden"}
            adapter.deliver(token, _json.dumps(bad, ensure_ascii=False))

    def user_as_B(adapter, payload, token):
        adapter.deliver(token, "B 的产出：修复完成")

    return (ClipboardAdapter(lambda t: None, user_as_A),
            ClipboardAdapter(lambda t: None, user_as_B))


r, logs, _ = run_engine(*make_clip_pair())
assert r["status"] == "success" and r["rounds"] == 1, r
assert r["final_output"] == "B 的产出：修复完成"

# 12. F6 回归：超时后迟到的回传必须被丢弃，不得解除下一轮等待
mode = {"round": 1}


def late_notify(adapter, payload, token):
    if mode["round"] == 2:
        assert adapter.deliver(token - 1, "迟到的旧回复") is False, "旧 token 回传未被丢弃"
        assert adapter.deliver(token, "新一轮的真实回复") is True


late_ad = ClipboardAdapter(lambda t: None, late_notify)
try:
    late_ad.send("第一轮", 0.2)
    assert False, "应超时"
except TimeoutError:
    pass
assert late_ad.is_waiting() is False
assert late_ad.deliver(1, "超时后的迟到回传") is False, "超时后的回传未被丢弃"
mode["round"] = 2
assert late_ad.send("第二轮", 2.0) == "新一轮的真实回复"

# 13. F8 回归：等待前 abort 立即生效，不空等到 timeout
dead = ClipboardAdapter(lambda t: None, lambda *a: None)
dead.abort()
try:
    dead.send("x", 5.0)
    assert False, "abort 后 send 应立即抛 Aborted"
except Aborted:
    pass

# 14. F2 回归：脱敏在 JSON 结构上进行，数值型敏感值打码后 sent 仍是合法 JSON
f2_pack = {"goal": "修 bug", "inputs": ["报告", "sk-testabcd5678efgh", {"user_id": 13812345678}],
           "steps": ["s"], "acceptance": ["a"], "forbidden": ["f"], "meta": {"note": "手机 13912345678"}}
clean_obj, hits = sanitize_obj(f2_pack)
sent = json.dumps(clean_obj, ensure_ascii=False)
reloaded = json.loads(sent)  # 数值被打成裸文本时这里必炸（F2 核心）
assert hits >= 3, hits
assert "sk-testabcd5678efgh" not in sent and "13812345678" not in sent and "13912345678" not in sent
assert reloaded["goal"] == "修 bug" and reloaded["inputs"][2]["user_id"] == "[已脱敏:手机号]"
assert f2_pack["inputs"][1] == "sk-testabcd5678efgh"  # 原对象不被原地修改

# 15. F3 回归：review 阶段超时——已产生的任务包/发出内容/B 产出必须留痕，不得整轮写空
class TimeoutReviewA(FakeA):
    def send(self, payload, timeout):
        if payload.startswith("你是任务布置型 Agent A，现在验收"):
            raise TimeoutError("单轮超时")
        return super().send(payload, timeout)


r, logs, store = run_engine(TimeoutReviewA(bad_first=False), FakeB(),
                            {"max_rounds": 1, "round_timeout": 30})
rows = store.rounds(r["session_id"])
assert len(rows) == 1, rows
no, st, ms, tpk, snt, bout, vdt, stage = rows[0]
assert st == "timeout" and stage == "review", rows[0]
assert tpk and snt and bout and not vdt, "B 已产出却被丢弃：留痕字段为空"

# 16. F3：B 执行超时的轮次，task_pack/sent_to_b 已产生也必须留痕
r, logs, store = run_engine(FakeA(bad_first=False), FakeB("timeout"),
                            {"max_rounds": 1, "round_timeout": 30})
no, st, ms, tpk, snt, bout, vdt, stage = store.rounds(r["session_id"])[0]
assert st == "timeout" and stage == "execute"
assert tpk and snt and not bout, "超时轮已产生的 pack/sent 未留痕"

# 17. F3：A 出包失败轮，未产生的字段为 None（而非整轮空串），stage 标明进行到哪一步
r, logs, store = run_engine(HopelessA(), FakeB())
no, st, ms, tpk, snt, bout, vdt, stage = store.rounds(r["session_id"])[0]
assert st == "error" and stage == "plan" and tpk is None and snt is None

# 18. F4 回归：打回重试的 prompt 保留原始上下文，一次格式失误不判死会话
class BadReviewOnceA(FakeA):
    def send(self, payload, timeout):
        if payload.startswith("你是任务布置型 Agent A，现在验收"):
            if "你上次的输出不合法" not in payload:
                return "抱歉，这不是 JSON"  # 首次验收输出格式错误
            assert "B 的产出" in payload, "验收打回后丢失 B 产出"
            assert "修 bug" in payload, "验收打回后丢失任务包"
            return '{"passed": true, "comments": "通过"}'
        return super().send(payload, timeout)


r, logs, _ = run_engine(BadReviewOnceA(bad_first=False), FakeB())
assert r["status"] == "success" and r["rounds"] == 1, r

a4 = FakeA(verdicts=[(True, "通过")])
r, logs, _ = run_engine(a4, FakeB())
retries = [p for p in a4.prompts if "你上次的输出不合法" in p]
assert retries, "未发生打回"
assert all(p.startswith("你是任务布置型 Agent A") for p in retries), "打回后丢失原上下文"
assert any("用户指令" in p and "修复登录头像不刷新" in p for p in retries), "打回后丢失用户指令"

# 19. F5 回归：四种畸形响应归一为 AdapterError，run() 返回 failed 且 sessions 行完整收尾
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from adapters import ApiAdapter
import threading as _threading


def start_stub(body_obj):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps(body_obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    _threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/v1", srv.shutdown


for bad_label, bad_body in [
    ("顶层数组", [1, 2]),
    ("message 是字符串", {"choices": [{"message": "content 是字符串"}]}),
    ("content 为 null", {"choices": [{"message": {"content": None}}]}),
    ("choices 为空", {"choices": []}),
]:
    stub_url, stop_stub = start_stub(bad_body)
    r, logs, store = run_engine(ApiAdapter(stub_url, "k", "m"), FakeB())
    assert r["status"] == "failed", (bad_label, r)
    sess = store.get_session(r["session_id"])
    assert sess["status"] == "failed" and sess["ended_at"] and sess["final_output"] is None, bad_label
    assert any("API 响应" in m for m in logs), bad_label
    stop_stub()

print("OK — P0 全部自检通过")
