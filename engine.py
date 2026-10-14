"""任务引擎：任务包强制校验（缺字段打回 A 补全）、脱敏、轮次/超时/僵局控制、SQLite 留痕。

不感知任何具体 Agent——A/B 只要是实现了 send(payload, timeout) -> str 的适配器即可。
"""
import difflib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime

from adapters import Aborted, AdapterError

PACK_RETRIES = 3      # 任务包缺字段/非法时的打回补全总尝试数
DEADLOCK_SIM = 0.9    # 连续两次验收意见相似度阈值（> 判僵局）
DEFAULT_CONFIG = {"max_rounds": 5, "round_timeout": 600}

PLAN_PROMPT = """你是任务布置型 Agent A。把用户指令拆解成结构化任务包，交给执行型 Agent B 执行。
只输出一个 JSON 对象，不要任何多余文字。字段（verify 可选，其余必填）：
goal: 字符串，任务目标
inputs: 数组，输入材料
steps: 数组，执行步骤
acceptance: 数组，验收标准（可逐条检查）
forbidden: 数组，禁止事项
verify: 可选，数组，验收验证命令（每项一条 shell 命令）——想让验收基于真实执行而非 B 自述就给出，如 ["py scenarios/demo_project/test_calc.py"]；省略则不执行验证

用户指令：{directive}
{history}"""

REVIEW_PROMPT = """你是任务布置型 Agent A，现在验收执行型 Agent B 的产出。
只输出一个 JSON 对象：{{"passed": true 或 false, "comments": "逐条对照验收标准给出的验收意见"}}

任务包：{pack}
B 的产出：{output}{verify_block}"""

VERIFY_HEADER = "【执行验证结果（真实执行 B 修复后的代码所得，非 B 自述）】"
VERIFY_RULE = "【硬规则】若任一验证命令退出码非 0 或超时，不得判通过（passed 必须为 false），并在 comments 中引用失败原因。"


def _clip(s: str, limit: int = 2000) -> str:
    """stdout/stderr 截断到 limit 字符，防日志/留痕/prompt 爆炸（截断即注明）。"""
    return s if len(s) <= limit else s[:limit] + f"…（已截断，原 {len(s)} 字符）"


def _verify_evidence(verify_result) -> str:
    """verify 执行结果 -> 验收 prompt 证据段（每条命令一段 + 硬规则）。
    无验证结果返回空串：review prompt 与无 verify 时代逐字一致。"""
    if not verify_result:
        return ""
    parts = [VERIFY_HEADER]
    for r in verify_result:
        parts.append(f"命令: {r['command']}\n退出码: {r['exit_code']}\n"
                     f"stdout: {r['stdout'] or '（空）'}\nstderr: {r['stderr'] or '（空）'}")
    parts.append(VERIFY_RULE)
    return "\n" + "\n".join(parts)


class PackInvalid(RuntimeError):
    """A 连续 PACK_RETRIES 次给不出合法任务包。"""


class ReplanRequested(RuntimeError):
    """用户终止本轮并要求重新布置（需求 §9.2 / F20）。"""


# ---------- 任务包 / 验收结果校验 ----------

def extract_json(text: str) -> dict:
    """全文 raw_decode 扫描提取唯一 JSON 对象；多于一个即报错，不静默取第一个（F12）。"""
    decoder = json.JSONDecoder()
    found, idx = [], 0
    while True:
        i = text.find("{", idx)
        if i < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, i)
            found.append(obj)
            idx = end
        except ValueError:
            idx = i + 1
    if not found:
        raise ValueError("回复中找不到 JSON 对象")
    if len(found) > 1:
        raise ValueError(f"回复中包含 {len(found)} 个 JSON 对象，无法确定用哪个，请只输出一个")
    return found[0]


def validate_pack(obj) -> list:
    """必填字段校验（含类型，F11），返回错误列表（空 = 通过）。"""
    if not isinstance(obj, dict):
        return ["任务包必须是 JSON 对象"]
    errors = []
    goal = obj.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        errors.append("goal 必须为非空字符串")
    for f in ("inputs", "steps", "acceptance", "forbidden"):
        v = obj.get(f)
        if not isinstance(v, list) or not v:
            errors.append(f"缺少必填字段或类型不符（须非空数组）: {f}")
    verify = obj.get("verify")  # 可选字段：缺省 = 不验证（既有 mock/回归包均无 verify）
    if verify is not None:
        if (not isinstance(verify, list) or not verify
                or not all(isinstance(c, str) and c.strip() for c in verify)):
            errors.append("verify 若给出必须为非空字符串数组（每项一条 shell 命令）")
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


def _js(o):
    """dict -> JSON 字符串；None 保持 None（留痕中区分「未产生」与「空」）。"""
    return json.dumps(o, ensure_ascii=False) if o is not None else None


# ---------- SQLite 留痕 ----------

class Store:
    def __init__(self, path="data/relay.db"):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("""CREATE TABLE IF NOT EXISTS sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, directive TEXT, adapter_a TEXT, adapter_b TEXT,
            config TEXT, status TEXT, final_output TEXT, created_at TEXT, ended_at TEXT)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS rounds(
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER, round_no INTEGER,
            task_pack TEXT, sent_to_b TEXT, b_output TEXT, verdict TEXT,
            duration_ms INTEGER, status TEXT, started_at TEXT, stage TEXT, verify_result TEXT)""")
        self._db.commit()
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(rounds)")]
        for col in ("stage", "verify_result"):  # 旧库迁移：缺列则补
            if col not in cols:
                self._db.execute(f"ALTER TABLE rounds ADD COLUMN {col} TEXT")
                self._db.commit()

    def create_session(self, directive, adapter_a, adapter_b, config) -> int:
        cur = self._db.execute(
            "INSERT INTO sessions(directive, adapter_a, adapter_b, config, created_at) VALUES(?,?,?,?,?)",
            (directive, adapter_a, adapter_b, json.dumps(config), datetime.now().isoformat(timespec="seconds")))
        self._db.commit()
        return cur.lastrowid

    def log_round(self, sid, round_no, pack, sent, b_out, verdict, duration_ms, status,
                  started_at, stage, verify_result=None):
        """pack/sent/b_out/verdict 传 None 表示该数据在本轮中尚未产生（F3：已产生必须落库）。
        verify_result 为 verify 命令真实执行结果（JSON 字符串），本轮未执行验证时为 None。"""
        self._db.execute(
            "INSERT INTO rounds(session_id, round_no, task_pack, sent_to_b, b_output, verdict,"
            " duration_ms, status, started_at, stage, verify_result) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sid, round_no, pack, sent, b_out, verdict, duration_ms, status, started_at,
             stage, verify_result))
        self._db.commit()

    def finish_session(self, sid, status, final_output):
        self._db.execute("UPDATE sessions SET status=?, final_output=?, ended_at=? WHERE id=?",
                         (status, final_output, datetime.now().isoformat(timespec="seconds"), sid))
        self._db.commit()

    def get_session(self, sid):
        return self._db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()

    def sessions_list(self):
        return self._db.execute(
            "SELECT id, directive, status, created_at, ended_at FROM sessions"
            " ORDER BY id DESC").fetchall()

    def export_session(self, sid, path) -> str:
        """导出整会话留痕为 JSON。F21：task_pack 是未脱敏原文，导出前必须重新脱敏；
        b_output/verdict 等文本字段同样过一遍文本脱敏。"""
        sess = dict(self.get_session(sid))
        rows = []
        for r in self.rounds(sid):
            r = dict(r)
            if r["task_pack"]:
                try:
                    r["task_pack"] = json.dumps(sanitize_obj(json.loads(r["task_pack"]))[0],
                                                ensure_ascii=False)
                except (ValueError, json.JSONDecodeError):
                    r["task_pack"], _ = sanitize(r["task_pack"])
            for f in ("sent_to_b", "b_output", "verdict"):
                if r[f]:
                    r[f], _ = sanitize(r[f])
            rows.append(r)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"session": sess, "rounds": rows}, f, ensure_ascii=False, indent=2)
        return path

    def close(self):
        self._db.close()

    def rounds(self, sid):
        # ponytail: 不追加 verify_result 到 SELECT——test_p0 有 3 处按位置精确解包 8 列，
        # 加第 9 列即炸；verify_result 经 log_round 落库，需要时按列名单独查。
        return self._db.execute(
            "SELECT round_no, status, duration_ms, task_pack, sent_to_b, b_output, verdict, stage"
            " FROM rounds WHERE session_id=? ORDER BY round_no", (sid,)).fetchall()


# ---------- 引擎 ----------

class Engine:
    def __init__(self, adapter_a, adapter_b, store: Store, config=None):
        self.a, self.b, self.store = adapter_a, adapter_b, store
        self.cfg = {**DEFAULT_CONFIG, **(config or {})}
        self._stop = threading.Event()
        self._replan = threading.Event()

    def stop(self):
        self._stop.set()

    def request_replan(self):
        """用户终止本轮、要求 A 重新布置（F20）。在轮内检查点生效，作废当前轮进入下一轮。"""
        self._replan.set()

    def _check_cancel(self):
        """轮内取消检查点：终止优先于重新布置。"""
        if self._stop.is_set():
            raise Aborted("用户终止")
        if self._replan.is_set():
            self._replan.clear()
            raise ReplanRequested("用户终止本轮，要求重新布置")

    def _remaining(self, t0):
        left = self.cfg["round_timeout"] - (time.monotonic() - t0)
        if left <= 0:
            raise TimeoutError("单轮超时")
        return left

    def _run_verify(self, commands, t0) -> list:
        """B 产出后、A 验收前，逐条真实执行任务包的 verify 命令（shell）。
        每条收集命令/退出码/截断后的 stdout/stderr；超时记退出码 "timeout"（进程由
        subprocess.run 负责杀）；单条 OSError 记退出码 -1，不让整个轮崩。
        PYTHONUTF8=1 让 py 子进程管道输出统一 UTF-8，与 utf-8 解码配套（跨机器确定性）。"""
        results = []
        for cmd in commands:
            try:
                r = subprocess.run(cmd, shell=True, capture_output=True,
                                   cwd=self.cfg.get("verify_cwd") or None,
                                   timeout=self._remaining(t0),
                                   env={**os.environ, "PYTHONUTF8": "1"},
                                   encoding="utf-8", errors="replace")
                code, out, err = r.returncode, r.stdout or "", r.stderr or ""
            except (subprocess.TimeoutExpired, TimeoutError):
                code, out, err = "timeout", "", "验证命令超时，已被终止"
            except OSError as e:
                code, out, err = -1, "", f"验证命令无法执行: {e}"
            results.append({"command": cmd, "exit_code": code,
                            "stdout": _clip(out), "stderr": _clip(err)})
        return results

    def _get_json(self, build_prompt, validate, log, tag, remaining):
        """调 A 并校验其 JSON 输出；缺字段打回补全，最多 PACK_RETRIES 次。
        打回时在原 prompt 后追加错误说明——无状态 API 的 A 只有拿到完整上下文才能补全（F4）。
        remaining 是 callable：每次重试前重新计算单轮剩余预算（F9）。"""
        original = prompt = build_prompt()
        last_err = None
        for _ in range(PACK_RETRIES):
            self._check_cancel()
            raw = self.a.send(prompt, remaining())
            try:
                obj = extract_json(raw)
                errors = validate(obj)
            except (ValueError, json.JSONDecodeError) as e:
                errors = [f"JSON 解析失败: {e}"]
            if not errors:
                return obj
            last_err = "；".join(errors)
            log(f"{tag}被打回：{last_err}")
            prompt = original + f"\n\n[校验失败] 你上次的输出不合法：{last_err}。请重新输出，只输出完整合法的 JSON。"
        raise PackInvalid(f"A 连续 {PACK_RETRIES} 次输出不合法（{tag}）：{last_err}")

    def run(self, directive: str, log=print) -> dict:
        self._stop.clear()  # 实例可复用：上一次会话的终止标记不带入本次（F15）
        sid = self.store.create_session(directive, self.a.label, self.b.label, self.cfg)
        verdicts, outputs, status, final, rnd = [], [], "max_rounds", None, 0
        executed = 0
        last_replan = False
        log(f"会话 #{sid} 开始：{directive}")
        try:
            for rnd in range(1, self.cfg["max_rounds"] + 1):
                if self._stop.is_set():
                    status = "aborted"
                    break
                executed = rnd  # 实际开始执行的轮数（首轮前中止时保持 0，F15）
                t0 = time.monotonic()
                started = datetime.now().isoformat(timespec="seconds")
                pack = sent = b_out = verify_result = None
                stage = "plan"
                replan_hint = last_replan
                last_replan = False
                try:
                    history = ("上一轮被用户终止，请重新布置任务包。" if replan_hint else
                               f"上一轮验收未通过，意见：{verdicts[-1]}\n请重新布置任务包。"
                               if verdicts else "")
                    pack = self._get_json(
                        lambda: PLAN_PROMPT.format(directive=directive, history=history),
                        validate_pack, log, "任务包", lambda: self._remaining(t0))
                    stage = "execute"
                    pack["meta"] = {"round": rnd, "created_at": started}
                    sent_obj, hits = sanitize_obj(pack)
                    if hits:
                        log(f"第 {rnd} 轮：发给 B 前脱敏 {hits} 处")
                    sent = json.dumps(sent_obj, ensure_ascii=False, indent=2)
                    b_out = self.b.send(sent, self._remaining(t0))
                    self._check_cancel()  # B 执行中收到重新布置请求：产出留痕后作废本轮（F20）
                    verify_result = None
                    if pack.get("verify"):
                        # B 自述不可信：验收前先真实执行验证命令，证据注入 review prompt
                        verify_result = self._run_verify(pack["verify"], t0)
                        bad = sum(1 for r in verify_result if r["exit_code"] != 0)
                        log(f"第 {rnd} 轮：真实执行验证 {len(verify_result)} 条命令，"
                            f"{'全部退出码 0' if not bad else f'{bad} 条失败（证据已注入验收）'}")
                    verify_block = _verify_evidence(verify_result)
                    stage = "review"
                    verdict = self._get_json(
                        lambda: REVIEW_PROMPT.format(pack=json.dumps(pack, ensure_ascii=False),
                                                     output=b_out, verify_block=verify_block),
                        validate_verdict, log, "验收结果", lambda: self._remaining(t0))
                    stage = "done"
                except TimeoutError:
                    self.store.log_round(sid, rnd, _js(pack), sent, b_out, None,
                                         int((time.monotonic() - t0) * 1000), "timeout", started, stage,
                                         _js(verify_result))
                    log(f"第 {rnd} 轮：单轮超时（{self.cfg['round_timeout']}s，进行到 {stage}），进入下一轮")
                    continue
                except Aborted:
                    status = "aborted"
                    break
                except ReplanRequested as e:
                    self.store.log_round(sid, rnd, _js(pack), sent, b_out, None,
                                         int((time.monotonic() - t0) * 1000), "replan", started, stage,
                                         _js(verify_result))
                    log(f"第 {rnd} 轮：{e}（已产生的数据已留痕）")
                    last_replan = True
                    continue
                except (PackInvalid, AdapterError) as e:
                    self.store.log_round(sid, rnd, _js(pack), sent, b_out, None,
                                         int((time.monotonic() - t0) * 1000), "error", started, stage,
                                         _js(verify_result))
                    log(f"第 {rnd} 轮中止（进行到 {stage}）：{e}")
                    status = "failed"
                    break
                duration = int((time.monotonic() - t0) * 1000)
                self.store.log_round(sid, rnd, _js(pack), sent, b_out,
                                     json.dumps(verdict, ensure_ascii=False), duration, "ok", started, "done",
                                     _js(verify_result))
                verdicts.append(verdict["comments"])
                outputs.append(b_out)
                log(f"第 {rnd} 轮：验收{'通过 ✓' if verdict['passed'] else '不通过 ✗'} — {verdict['comments']}")
                if verdict["passed"]:
                    status, final = "success", b_out
                    break
                if (len(verdicts) >= 2 and similarity(verdicts[-1], verdicts[-2]) > DEADLOCK_SIM
                        and similarity(outputs[-1], outputs[-2]) > DEADLOCK_SIM):
                    # 僵局 = 意见与 B 产出连续两轮均无实质变化；只看意见会误杀
                    # 「验收标准第 1/2 条未满足」这类措辞相近但 B 在改进的场景（F13）
                    status = "deadlock"
                    log("僵局：连续两轮验收意见与 B 产出均无实质变化，终止，请重新布置")
                    break
            else:
                log(f"已达最大轮数 {self.cfg['max_rounds']}，终止")
        except Exception as e:  # 兜底（F5）：任何未预期异常也必须收尾会话，不留悬挂行
            status = "failed"
            log(f"引擎异常终止：{type(e).__name__}: {e}")
        finally:
            self.store.finish_session(sid, status, final)
        log(f"会话 #{sid} 结束：{status}（共 {executed} 轮）")
        return {"session_id": sid, "status": status, "rounds": executed, "final_output": final}
