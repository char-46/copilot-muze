import re

from muze.config import MuzeConfig, describe_watch, is_control_conversation
from muze.store.media import safe_name, to_file_uri
from muze.version import BASE_VERSION, get_version

VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?\+build\.g[0-9a-f]+(\.dirty)?\.\d{8}T\d{6}Z$"
)


def test_version_semver_shape():
    v = get_version()
    assert v.startswith(BASE_VERSION)
    assert VERSION_RE.match(v), v


def test_watch_rules():
    cfg = MuzeConfig()
    cfg.ingest.scope = "all"
    assert describe_watch(cfg, "friend", 1) is True

    cfg.ingest.scope = "watchlist"
    cfg.ingest.watchlist.friends = [11]
    cfg.ingest.watchlist.groups = [22]
    cfg.control.admin_users = [33]
    assert describe_watch(cfg, "friend", 11) is True
    assert describe_watch(cfg, "group", 22) is True
    assert describe_watch(cfg, "friend", 11 + 1) is False
    # control conversations are always watched
    assert describe_watch(cfg, "friend", 33) is True


def test_control_conversation():
    cfg = MuzeConfig()
    cfg.control.admin_users = [7]
    cfg.control.control_group = 9
    assert is_control_conversation(cfg, "friend", 7) is True
    assert is_control_conversation(cfg, "group", 9) is True
    assert is_control_conversation(cfg, "group", 8) is False


def test_to_file_uri_windows():
    uri = to_file_uri(r"A:\data\media\x.jpg")
    assert uri.startswith("file:///") and "A:/data/media/x.jpg" in uri


def test_safe_name():
    assert safe_name("a b/c:d", "fb") == "a_b_c_d"
    assert safe_name("", "fb") == "fb"
