"""accounts/authority config layout + per-uin data dirs + cfg dotted get/set."""

import pytest

from muze.cfgcli import dotted_get, dotted_set, _mask
from muze.config import load_accounts


def write_cfg(tmp_path, text):
    p = tmp_path / "accounts.yaml"
    p.write_text(text, encoding="utf-8")
    return p


SINGLE = """
control: {controller: [10000]}
accounts:
  milky:
    - base_url: "http://127.0.0.1:3010"
storage: {data_dir: ./d}
"""

MULTI = """
control: {controller: [10000]}
llm:
  chat: {base_url: "https://x", api_key: k, model: m}
digest:
  enabled: false
accounts:
  milky:
    - name: main
      base_url: "http://127.0.0.1:3010"
    - name: alt
      base_url: "http://127.0.0.1:3011"
      controller: [20002]
      digest: {enabled: true}
"""

LEGACY = """
authority: [10000]
accounts:
  milky:
    - base_url: "http://127.0.0.1:3010"
    - base_url: "http://127.0.0.1:3011"
      authority: [20002]
"""


def test_load_accounts_single(tmp_path):
    specs = load_accounts(write_cfg(tmp_path, SINGLE))
    assert len(specs) == 1
    s = specs[0]
    assert s.protocol == "milky"
    assert s.name == "milky-1"  # default name
    assert s.endpoint.base_url.endswith("3010")
    assert s.config.control.controller == [10000]


def test_load_accounts_multi_inherit_and_union(tmp_path):
    specs = load_accounts(write_cfg(tmp_path, MULTI))
    assert [s.name for s in specs] == ["main", "alt"]
    main, alt = specs
    # shared llm inherited by both
    assert main.config.llm.chat.model == "m" and alt.config.llm.chat.model == "m"
    # per-account override via deep-merge
    assert main.config.digest.enabled is False
    assert alt.config.digest.enabled is True
    # controller: global list unions the entry's extra list
    assert main.config.control.controller == [10000]
    assert alt.config.control.controller == [10000, 20002]
    # endpoints are separate objects
    assert main.endpoint is not alt.endpoint
    assert alt.endpoint.base_url.endswith("3011")


def test_load_accounts_legacy_authority_still_works(tmp_path):
    """The old top-level `authority:` and per-account `authority:` are folded
    into control.controller (soft migration)."""
    specs = load_accounts(write_cfg(tmp_path, LEGACY))
    assert specs[0].config.control.controller == [10000]
    assert specs[1].config.control.controller == [10000, 20002]


def test_load_accounts_duplicate_name(tmp_path):
    p = write_cfg(
        tmp_path,
        "accounts:\n  milky:\n    - {name: a, base_url: 'http://x'}\n    - {name: a, base_url: 'http://y'}\n",
    )
    with pytest.raises(ValueError):
        load_accounts(p)


def test_reject_legacy_layout(tmp_path):
    p = write_cfg(tmp_path, "milky:\n  base_url: http://127.0.0.1:3010\n")
    with pytest.raises(ValueError, match="accounts.milky"):
        load_accounts(p)
    p = write_cfg(
        tmp_path,
        "instances:\n  - {milky: {base_url: 'http://x'}}\n",
    )
    with pytest.raises(ValueError, match="不兼容旧版"):
        load_accounts(p)


def test_reject_unsupported_protocols_and_web_override(tmp_path):
    p = write_cfg(
        tmp_path,
        "accounts:\n  milky: [{base_url: 'http://x'}]\n  ob11: [{base_url: 'http://y'}]\n",
    )
    with pytest.raises(ValueError, match="ob11"):
        load_accounts(p)
    p = write_cfg(
        tmp_path,
        "accounts:\n  milky: [{base_url: 'http://x', web: {port: 9000}}]\n",
    )
    with pytest.raises(ValueError, match="应用级"):
        load_accounts(p)


def test_empty_milky_rejected(tmp_path):
    p = write_cfg(tmp_path, "accounts:\n  milky: []\n")
    with pytest.raises(ValueError, match="至少需要一项"):
        load_accounts(p)


def test_account_dir_per_uin(tmp_path):
    specs = load_accounts(write_cfg(tmp_path, SINGLE))
    storage = specs[0].config.storage
    storage.data_dir = tmp_path / "root"
    acct = storage.account_dir(12345)
    assert acct == storage.data_dir / "12345"
    assert acct.is_dir()
    # second account gets its own dir automatically
    other = storage.account_dir(67890)
    assert other != acct
    assert other == storage.data_dir / "67890"


def test_is_control_conversation_uses_controller():
    from muze.config import MuzeConfig, is_control_conversation

    cfg = MuzeConfig.model_validate({"control": {"controller": [10001]}})
    assert is_control_conversation(cfg, "friend", 10001)
    assert not is_control_conversation(cfg, "friend", 10002)
    cfg2 = MuzeConfig.model_validate(
        {"control": {"controller": [1], "control_group": 42}}
    )
    assert is_control_conversation(cfg2, "group", 42)


def test_dotted_get_set():
    raw = {"accounts": {"milky": [{"base_url": "u", "access_token": "s"}]}, "x": 1}
    assert dotted_get(raw, "accounts.milky.0.base_url") == "u"
    dotted_set(raw, "accounts.milky.0.base_url", "http://new")
    assert raw["accounts"]["milky"][0]["base_url"] == "http://new"
    dotted_set(raw, "accounts.milky.1.base_url", "http://b")  # auto-extend list
    assert raw["accounts"]["milky"][1]["base_url"] == "http://b"
    dotted_set(raw, "x", "42")  # int coercion follows existing value
    assert raw["x"] == 42


def test_mask_secrets_anywhere():
    masked = _mask(
        {
            "accounts": {"milky": [{"access_token": "top"}]},
            "llm": {"chat": {"api_key": "k"}},
            "web": {"token": "t"},
        }
    )
    assert masked["accounts"]["milky"][0]["access_token"] == "***set***"
    assert masked["llm"]["chat"]["api_key"] == "***set***"
    assert masked["web"]["token"] == "***set***"
