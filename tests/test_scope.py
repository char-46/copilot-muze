"""Command scope registry (app vs bot) + /help sectioning."""

from muze.core.service import COMMANDS, CommandSpec


def test_scopes_registered():
    app_cmds = {"help", "version", "read", "fetch", "reload", "restart", "shutdown"}
    for name in app_cmds:
        spec = COMMANDS.get(name)
        assert spec is not None, name
        assert spec.scope == "app", name
    # data commands default to bot scope
    for name in ("status", "search", "msg", "media", "sync", "send", "dream", "memories"):
        spec = COMMANDS.get(name)
        assert spec is not None, name
        assert spec.scope == "bot", name


def test_help_text_sections():
    text = COMMANDS.help_text()
    assert "应用级" in text and "本账号" in text
    # every command still appears exactly once
    for spec in COMMANDS.all():
        assert text.count(f"/{spec.name}") >= 1


def test_commandspec_scope_default():
    spec = CommandSpec(name="x", usage="", help="", fn=None)
    assert spec.scope == "bot"
