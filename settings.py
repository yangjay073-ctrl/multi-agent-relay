"""设置持久化：profile 池（多候选，F19）+ A/B 绑定 + 循环参数。存本地 data/settings.json。"""
import json
import os
import subprocess
import sys

SETTINGS_PATH = "data/settings.json"

DEFAULTS = {
    "profiles": {},                # 名字 -> {type: api|clipboard, base_url, api_key, model, launch_command}
    "bind": {"A": "", "B": ""},    # 当前绑定的 profile 名（或内建 "本地Mock(测试用)"）
    "max_rounds": 5,
    "round_timeout": 600,
}


def _warn_if_tracked(path):
    """A6 防误提交提示：文件存在、未被 .gitignore 覆盖、且已被 git 跟踪 → stderr 醒目警告。

    文件不存在（:memory:、临时路径等）直接跳过；无 git / 不在仓库 / 任何 git 故障静默跳过；
    只警告不抛异常，绝不影响 load 本身。
    """
    try:
        if not os.path.isfile(path):
            return
        ignored = subprocess.run(["git", "check-ignore", "-q", path], capture_output=True)
        if ignored.returncode == 0:  # 已被 .gitignore 覆盖 → 安全
            return
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", path],
                                 capture_output=True)
        if tracked.returncode == 0:  # 未被忽略却已被跟踪 → 改动会随 git add 进入提交
            print(f"[安全警告] {path} 含明文密钥且已被 git 跟踪，极可能被提交！"
                  f"请立即 git rm --cached 并加入 .gitignore。", file=sys.stderr)
    except Exception:
        pass  # 无 git 环境等一切故障：静默跳过


def load(path=SETTINGS_PATH) -> dict:
    _warn_if_tracked(path)
    cfg = {k: (json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v)
           for k, v in DEFAULTS.items()}
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        cfg.update({k: v for k, v in saved.items() if k in cfg})
        cfg["bind"].update(saved.get("bind", {}))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save(cfg: dict, path=SETTINGS_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
