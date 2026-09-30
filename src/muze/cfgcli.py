"""`muze cfg` — quick config management from the terminal.

    muze cfg                     # show whole config (secrets masked)
    muze cfg show milky.base_url # show one dotted path (instances: instances.0.milky...)
    muze cfg set <path> <value>  # edit config.yaml in place (comments are lost)
    muze cfg init                # interactive wizard writing a minimal config

Note: `set` rewrites the YAML file; comments are not preserved. For
comment-rich configs edit the file by hand or use the webui settings page
(runtime apply) + /reload.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

MASK = "***set***"
SECRET_KEYS = {"access_token", "api_key", "token"}


def _mask(node, key="") -> object:
    if isinstance(node, dict):
        return {k: _mask(v, k) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask(v) for v in node]
    if key in SECRET_KEYS and isinstance(node, str) and node:
        return MASK
    return node


def dotted_get(node, dotted: str):
    for part in dotted.split("."):
        if part == "":
            continue
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def dotted_set(node, dotted: str, value) -> None:
    keys = [k for k in dotted.split(".") if k]
    if not keys:
        raise ValueError("empty path")
    for k in keys[:-1]:
        if isinstance(node, list):
            idx = int(k)
            while len(node) <= idx:
                node.append({})
            node = node[idx]
        elif isinstance(node, dict):
            nxt = node.get(k)
            if not isinstance(nxt, (dict, list)):
                nxt = {}
                node[k] = nxt
            node = nxt
        else:
            raise ValueError(f"cannot descend into {k!r}")
    leaf = keys[-1]
    if isinstance(node, list):
        idx = int(leaf)
        while len(node) <= idx:
            node.append({})
        node[idx] = _coerce(value, node[idx] if node[idx] else value)
    elif isinstance(node, dict):
        node[leaf] = _coerce(value, node.get(leaf))
    else:
        raise ValueError(f"cannot set {leaf!r}")


def _coerce(text: str, like) -> object:
    if isinstance(like, bool):
        return str(text).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int) and str(text).strip().lstrip("-").isdigit():
        return int(text)
    if isinstance(like, list):
        if str(text).strip().startswith("["):
            try:
                return yaml.safe_load(text)
            except Exception:
                pass
        return [x.strip() for x in str(text).replace("，", ",").split(",") if x.strip()]
    if isinstance(like, float):
        try:
            return float(text)
        except ValueError:
            return text
    return text


def _load_raw(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return raw if isinstance(raw, dict) else {}


def _dump(raw: dict, path: Path) -> None:
    path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def run_cfg(argv: list[str]) -> int:
    # pre-extract -c/--config so it can appear anywhere: muze cfg show x -c f.yaml
    config_file = "config.yaml"
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-c", "--config"):
            if i + 1 < len(argv):
                config_file = argv[i + 1]
                i += 2
                continue
            print("-c requires a value", file=sys.stderr)
            return 2
        if a.startswith("--config="):
            config_file = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1

    p = argparse.ArgumentParser(prog="muze cfg", description="quick config management")
    sub = p.add_subparsers(dest="cmd")
    showp = sub.add_parser("show", help="show config (dotted path optional)")
    showp.add_argument("path", nargs="?", default="")
    setp = sub.add_parser("set", help="set a dotted path value")
    setp.add_argument("path")
    setp.add_argument("value")
    sub.add_parser("init", help="interactive wizard")
    args = p.parse_args(rest)

    path = Path(config_file)
    cmd = args.cmd

    if cmd == "show":
        raw = _load_raw(path)
        if not raw:
            print(f"(empty config: {path})", flush=True)
            return 0
        if args.path:
            val = dotted_get(_mask(raw), args.path)
            print(val if val is not None else f"<unset: {args.path}>", flush=True)
        else:
            print(yaml.safe_dump(_mask(raw), allow_unicode=True, sort_keys=False), flush=True)
        return 0

    if cmd == "set":
        raw = _load_raw(path)
        try:
            current = dotted_get(raw, args.path)
        except Exception:
            current = None
        dotted_set(raw, args.path, args.value)
        _dump(raw, path)
        print(f"set {args.path} = {args.value}  (file rewritten; comments lost)", flush=True)
        return 0

    if cmd == "init":
        return _wizard(path)
    return 2


def _prompt(label: str, default: str = "") -> str:
    try:
        v = input(f"{label}[{default}]: ").strip()
    except EOFError:
        v = ""
    return v or default


def _wizard(path: Path) -> int:
    if path.exists():
        ans = _prompt(f"config {path} 已存在，覆盖? y/N", "n").lower()
        if ans not in ("y", "yes"):
            print("aborted", flush=True)
            return 0

    print("== muze cfg init：生成最小可用配置（回车取默认值）==", flush=True)
    base_url = _prompt("协议端(milky)地址", "http://127.0.0.1:8080")
    token = _prompt("协议端 access_token（空=无）", "")
    admin = _prompt("控制者QQ号（逗号分隔，必填）", "")
    if not admin:
        print("控制者QQ号必填", file=sys.stderr)
        return 2
    group = _prompt("控制群号（0=不用群）", "0")
    llm_url = _prompt("LLM base_url（OpenAI 兼容）", "https://open.bigmodel.cn/api/paas/v4/")
    llm_key = _prompt("LLM api_key", "")
    llm_model = _prompt("LLM model", "glm-4.7")
    vision = _prompt("视觉模型（空=禁用）", "")
    data_dir = _prompt("数据目录", "./data")
    multi = _prompt("多账号 instances 结构? y/N", "n").lower() in ("y", "yes")

    admin_users = [int(x) for x in admin.replace("，", ",").split(",") if x.strip()]
    inst = {
        "milky": {"base_url": base_url, "access_token": token},
        "storage": {"data_dir": data_dir},
        "control": {
            "admin_users": admin_users,
            "control_group": int(group) if group.isdigit() else 0,
        },
    }
    shared = {
        "llm": {
            "chat": {"base_url": llm_url, "api_key": llm_key, "model": llm_model},
            "vision": {"model": vision},
        },
        "digest": {"enabled": True, "at": "01:00"},
    }
    raw = {"instances": [ {"name": "main", **inst} ]} if multi else {**inst, **shared}
    _dump(raw, path)
    print(f"written: {path}\n下一步: muze daemon -c {path}", flush=True)
    return 0
