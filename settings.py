"""设置持久化：profile 池（多候选，F19）+ A/B 绑定 + 循环参数。存本地 data/settings.json。"""
import json
import os

SETTINGS_PATH = "data/settings.json"

DEFAULTS = {
    "profiles": {},                # 名字 -> {type: api|clipboard, base_url, api_key, model, launch_command}
    "bind": {"A": "", "B": ""},    # 当前绑定的 profile 名（或内建 "本地Mock(测试用)"）
    "max_rounds": 5,
    "round_timeout": 600,
}


def load(path=SETTINGS_PATH) -> dict:
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
