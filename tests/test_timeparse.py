"""Duration parsing for /sync 2d-style time windows."""

from muze.timeparse import human_duration, parse_duration


def test_parse_duration_units():
    assert parse_duration("2d") == 2 * 86400
    assert parse_duration("48h") == 48 * 3600
    assert parse_duration("30m") == 30 * 60
    assert parse_duration("90s") == 90
    assert parse_duration("1w") == 604800
    assert parse_duration("2 天") == 2 * 86400
    assert parse_duration("30分钟") == 30 * 60
    assert parse_duration("3小时") == 3 * 3600


def test_parse_duration_rejects_non_durations():
    for bad in ("", "force", "friend/1", "0d", "abc", "2x", "-1d", "99999d"):
        assert parse_duration(bad) is None


def test_human_duration():
    assert human_duration(2 * 86400) == "2 天"
    assert human_duration(48 * 3600) == "2 天"
    assert human_duration(90 * 60) == "90 分钟"
    assert human_duration(45) == "45 秒"
