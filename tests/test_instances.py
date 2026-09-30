"""Multi-instance config loading + cfg dotted get/set."""

import pytest

from muze.cfgcli import dotted_get, dotted_set, _mask
from muze.config import MuzeConfig, load_instances


def write_cfg(tmp_path, text):
    p = tmp_path / "multi.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_instances_single_compat(tmp_path):
    p = write_cfg(tmp_path, "milky:\n  base_url: http://127.0.0.1:3010\n")
    specs = load_instances(p)
    assert len(specs) == 1 and specs[0].name == "main"
    assert specs[0].config.milky.base_url == "http://127.0.0.1:3010"


def test_load_instances_multi_with_inheritance(tmp_path):
    p = write_cfg(
        tmp_path,
        """
llm:
  chat: {base_url: "https://x", api_key: k, model: m}
digest:
  enabled: false
instances:
  - name: main
    milky: {base_url: "http://127.0.0.1:3010"}
    storage: {data_dir: ./data-a}
    web: {port: 8300}
  - name: alt
    milky: {base_url: "http://127.0.0.1:3011"}
    storage: {data_dir: ./data-b, db_file: ./data-b/alt.db}
    web: {port: 8301}
    digest: {enabled: true}
""",
    )
    specs = load_instances(p)
    assert [s.name for s in specs] == ["main", "alt"]
    main, alt = specs
    # shared llm inherited by both
    assert main.config.llm.chat.model == "m" and alt.config.llm.chat.model == "m"
    # per-instance overrides
    assert main.config.milky.base_url.endswith("3010")
    assert alt.config.milky.base_url.endswith("3011")
    # deep-merge: alt re-enables digest, main keeps top-level default
    assert main.config.digest.enabled is False
    assert alt.config.digest.enabled is True
    # explicit db file
    assert str(alt.config.storage.db_path).endswith("alt.db")
    assert main.config.storage.db_file == ""


def test_load_instances_duplicate_name(tmp_path):
    p = write_cfg(
        tmp_path,
        "instances:\n  - {name: a, milky: {base_url: 'http://x'}}\n  - {name: a, milky: {base_url: 'http://y'}}\n",
    )
    with pytest.raises(ValueError):
        load_instances(p)


def test_storage_db_file_override():
    cfg = MuzeConfig.model_validate(
        {"storage": {"data_dir": "./d", "db_file": "custom.db"}}
    )
    assert cfg.storage.db_path.name == "custom.db"
    assert cfg.storage.db_file == "custom.db"


def test_dotted_get_set():
    raw = {"instances": [{"milky": {"base_url": "u", "access_token": "s"}}], "x": 1}
    assert dotted_get(raw, "instances.0.milky.base_url") == "u"
    dotted_set(raw, "instances.0.milky.base_url", "http://new")
    assert raw["instances"][0]["milky"]["base_url"] == "http://new"
    dotted_set(raw, "instances.1.milky.base_url", "http://b")
    assert raw["instances"][1]["milky"]["base_url"] == "http://b"
    # type coercion follows the existing value
    dotted_set(raw, "x", "42")
    assert raw["x"] == 42


def test_mask_secrets():
    masked = _mask({"milky": {"access_token": "top"}, "llm": {"chat": {"api_key": "k"}}})
    assert masked["milky"]["access_token"] == "***set***"
    assert masked["llm"]["chat"]["api_key"] == "***set***"
