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


def test_parse_duration_compound_and_calendar_units():
    assert parse_duration("1d12h") == 86400 + 12 * 3600
    assert parse_duration("1h30m") == 3600 + 30 * 60
    assert parse_duration("1d 12h 30m") == 86400 + 12 * 3600 + 30 * 60
    assert parse_duration("1mo") == 2592000  # 30 d
    assert parse_duration("2周3天") == 2 * 604800 + 3 * 86400
    assert parse_duration("1年") == 31536000


def test_parse_duration_rejects_non_durations():
    for bad in (
        "", "force", "friend/1", "0d", "abc", "2x", "-1d", "99999d",
        "1dfoo", "d1", "1dd", "1 2d",
    ):
        assert parse_duration(bad) is None


def test_human_duration():
    assert human_duration(2 * 86400) == "2 天"
    assert human_duration(48 * 3600) == "2 天"
    assert human_duration(90 * 60) == "90 分钟"
    assert human_duration(45) == "45 秒"
    assert human_duration(2592000) == "1 月"
    assert human_duration(31536000) == "1 年"

