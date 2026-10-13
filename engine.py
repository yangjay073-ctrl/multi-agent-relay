"""任务引擎：任务包强制校验（缺字段打回 A 补全）、脱敏、轮次/超时/僵局控制、SQLite 留痕。

不感知任何具体 Agent——A/B 只要是实现了 send(payload, timeout) -> str 的适配器即可。
"""
import difflib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime

from adapters import Aborted, AdapterError

REQUIRED = ("goal", "inputs", "steps", "acceptance", "forbidden")
PACK_RETRIES = 3      # 任务包缺字段/非法时的打回补全总尝试数
DEADLOCK_SIM = 0.9    # 连续两次验收意见相似度阈值（> 判僵局）
DEFAULT_CONFIG = {"max_rounds": 5, "round_timeout": 600}

PLAN_PROMPT = """你是任务布置型 Agent A。把用户指令拆解成结构化任务包，交给执行型 Agent B 执行。
只输出一个 JSON 对象，不要任何多余文字。字段（全部必填）：
goal: 字符串，任务目标
inputs: 数组，输入材料
steps: 数组，执行步骤
acceptance: 数组，验收标准（可逐条检查）
forbidden: 数组，禁止事项

用户指令：{directive}
{history}"""

REVIEW_PROMPT = """你是任务布置型 Agent A，现在验收执行型 Agent B 的产出。
只输出一个 JSON 对象：{{"passed": true 或 false, "comments": "逐条对照验收标准给出的验收意见"}}

任务包：{pack}
B 的产出：{output}"""


class PackInvalid(RuntimeError):
    """A 连续 PACK_RETRIES 次给不出合法任务包。"""


# ---------- 任务包 / 验收结果校验 ----------

def extract_json(text: str) -> dict:
    """从 A 的回复中提取 JSON（容忍 markdown 围栏与前后废话）。"""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("回复中找不到 JSON 对象")
    return json.loads(text[start:end + 1])


def validate_pack(obj) -> list:
    """必填字段校验，返回错误列表（空 = 通过）。"""
    if not isinstance(obj, dict):
        return ["任务包必须是 JSON 对象"]
    errors = []
    for f in REQUIRED:
        v = obj.get(f)
        empty = v is None or v == "" or v == [] or (isinstance(v, str) and not v.strip())
        if empty:
            errors.append(f"缺少必填字段或为空: {f}")
    return errors


def validate_verdict(obj) -> list:
    if not isinstance(obj, dict):
        return ["验收结果必须是 JSON 对象"]
    errors = []
    if not isinstance(obj.get("passed"), bool):
        errors.append('缺少布尔字段 "passed"')
    if not isinstance(obj.get("comments"), str) or not obj["comments"].strip():
        errors.append('缺少非空字符串字段 "comments"')
    return errors


# ---------- 脱敏（发给 B 之前） ----------

REDACTIONS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.+?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[已脱敏:私钥]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[已脱敏:api_key]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"), "[已脱敏:token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[已脱敏:access_key]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}"), "[已脱敏:bearer]"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "[已脱敏:手机号]"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "[已脱敏:身份证]"),
]


def _redact(text: str):
    hits = 0
    for pat, repl in REDACTIONS:
        text, n = pat.subn(repl, text)
        hits += n
    return text, hits


def sanitize(text: str):
    """整段文本脱敏，返回 (脱敏后文本, 命中数)。"""
    return _redact(text)


def sanitize_obj(obj):
    """在 JSON 结构上脱敏（F2）：字符串/数值叶子过正则，数值命中转为带引号的
    占位字符串，保证序列化后仍是合法 JSON。返回 (新结构, 命中数)，不改原对象。"""
    hits = 0

    def walk(v):
        nonlocal hits
        if isinstance(v, str):
            new, n = _redact(v)
            hits += n
            return new
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, (int, float)):
            new, n = _redact(str(v))
            if n:
                hits += n
                return new  # 数值敏感值 -> "[已脱敏:xx]" 字符串，避免裸文本破坏 JSON
            return v
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    return walk(obj), hits


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------- SQLite 留痕 ----------

class Store:
    def __init__(self, path="data/relay.db"):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("""CREATE TABLE IF NOT EXISTS sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, directive TEXT, adapter_a TEXT, adapter_b TEXT,
            config TEXT, status TEXT, final_output TEXT, created_at TEXT, ended_at TEXT)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS rounds(
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, round_no INTEGER,
            task_pack TEXT, sent_to_b TEXT, b_output TEXT, verdict TEXT,
            duration_ms INTEGER, status TEXT, started_at TEXT)""")
        self._db.commit()

    def create_session(self, directive, adapter_a, adapter_b, config) -> int:
        cur = self._db.execute(
            "INSERT INTO sessions(directive, adapter_a, adapter_b, config, created_at) VALUES(?,?,?,?,?)",
            (directive, adapter_a, adapter_b, json.dumps(config), datetime.now().isoformat(timespec="seconds")))
        self._db.commit()
        return cur.lastrowid

    def log_round(self, sid, round_no, pack, sent, b_out, verdict, duration_ms, status, started_at):
        self._db.execute(
            "INSERT INTO rounds(session_id, round_no, task_pack, sent_to_b, b_output, verdict,"
            " duration_ms, status, started_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, round_no, pack, sent, b_out, verdict, duration_ms, status, started_at))
        self._db.commit()

    def finish_session(self, sid, status, final_output):
        self._db.execute("UPDATE sessions SET status=?, final_output=?, ended_at=? WHERE id=?",
                         (status, final_output, datetime.now().isoformat(timespec="seconds"), sid))
        self._db.commit()

    def close(self):
        self._db.close()

    def rounds(self, sid):
        return self._db.execute(
            "SELECT round_no, status, duration_ms, task_pack, sent_to_b, b_output, verdict"
            " FROM rounds WHERE session_id=? ORDER BY round_no", (sid,)).fetchall()


# ---------- 引擎 ----------

class Engine:
    def __init__(self, adapter_a, adapter_b, store: Store, config=None):
        self.a, self.b, self.store = adapter_a, adapter_b, store
        self.cfg = {**DEFAULT_CONFIG, **(config or {})}
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def _remaining(self, t0):
        left = self.cfg["round_timeout"] - (time.monotonic() - t0)
        if left <= 0:
            raise TimeoutError("单轮超时")
        return left

    def _get_json(self, build_prompt, validate, log, tag, timeout):
        """调 A 并校验其 JSON 输出；缺字段打回补全，最多 PACK_RETRIES 次。"""
        prompt = build_prompt()
        last_err = None
        for _ in range(PACK_RETRIES):
            if self._stop.is_set():
                raise Aborted("用户终止")
            raw = self.a.send(prompt, timeout)
            try:
                obj = extract_json(raw)
                errors = validate(obj)
            except (ValueError, json.JSONDecodeError) as e:
                errors = [f"JSON 解析失败: {e}"]
            if not errors:
                return obj
            last_err = "；".join(errors)
            log(f"{tag}被打回：{last_err}")
            prompt = f"你上次的输出不合法：{last_err}。请重新输出，只输出完整合法的 JSON。"
        raise PackInvalid(f"A 连续 {PACK_RETRIES} 次输出不合法（{tag}）：{last_err}")

    def run(self, directive: str, log=print) -> dict:
        sid = self.store.create_session(directive, self.a.label, self.b.label, self.cfg)
        verdicts, status, final, rnd = [], "max_rounds", None, 0
        log(f"会话 #{sid} 开始：{directive}")
        for rnd in range(1, self.cfg["max_rounds"] + 1):
            if self._stop.is_set():
                status = "aborted"
                break
            t0 = time.monotonic()
            started = datetime.now().isoformat(timespec="seconds")
            try:
                history = f"上一轮验收未通过，意见：{verdicts[-1]}\n请重新布置任务包。" if verdicts else ""
                pack = self._get_json(
                    lambda: PLAN_PROMPT.format(directive=directive, history=history),
                    validate_pack, log, "任务包", self._remaining(t0))
                pack["meta"] = {"round": rnd, "created_at": started}
                sent_obj, hits = sanitize_obj(pack)
                if hits:
                    log(f"第 {rnd} 轮：发给 B 前脱敏 {hits} 处")
                sent = json.dumps(sent_obj, ensure_ascii=False, indent=2)
                b_out = self.b.send(sent, self._remaining(t0))
                verdict = self._get_json(
                    lambda: REVIEW_PROMPT.format(pack=json.dumps(pack, ensure_ascii=False),
                                                 output=b_out),
                    validate_verdict, log, "验收结果", self._remaining(t0))
            except TimeoutError:
                self.store.log_round(sid, rnd, "", "", "", "", int((time.monotonic() - t0) * 1000),
                                     "timeout", started)
                log(f"第 {rnd} 轮：单轮超时（{self.cfg['round_timeout']}s），进入下一轮")
                continue
            except Aborted:
                status = "aborted"
                break
            except (PackInvalid, AdapterError) as e:
                self.store.log_round(sid, rnd, "", "", "", "",
                                     int((time.monotonic() - t0) * 1000), "error", started)
                log(f"第 {rnd} 轮中止：{e}")
                status = "failed"
                break
            duration = int((time.monotonic() - t0) * 1000)
            self.store.log_round(sid, rnd, json.dumps(pack, ensure_ascii=False), sent, b_out,
                                 json.dumps(verdict, ensure_ascii=False), duration, "ok", started)
            verdicts.append(verdict["comments"])
            log(f"第 {rnd} 轮：验收{'通过 ✓' if verdict['passed'] else '不通过 ✗'} — {verdict['comments']}")
            if verdict["passed"]:
                status, final = "success", b_out
                break
            if len(verdicts) >= 2 and similarity(verdicts[-1], verdicts[-2]) > DEADLOCK_SIM:
                status = "deadlock"
                log("僵局：连续两次验收意见无实质变化（相似度 > 0.9），终止，请重新布置")
                break
        else:
            log(f"已达最大轮数 {self.cfg['max_rounds']}，终止")
        self.store.finish_session(sid, status, final)
        log(f"会话 #{sid} 结束：{status}（共 {rnd} 轮）")
        return {"session_id": sid, "status": status, "rounds": rnd, "final_output": final}
