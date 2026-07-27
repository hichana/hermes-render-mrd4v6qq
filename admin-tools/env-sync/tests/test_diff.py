from hermes_env_sync.diff import render
from hermes_env_sync.envfile import Diff


def test_secret_shaped_key_is_redacted():
    diff = Diff(changed=[("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_ACCESS_TOKEN=oldsecret",
                           "LINE_CHANNEL_ACCESS_TOKEN=newsecretvalue")])
    text = render(diff)
    assert "oldsecret" not in text
    assert "newsecretvalue" not in text
    assert "len=" in text
    assert "sha256=" in text


def test_non_secret_key_shown_in_full():
    diff = Diff(changed=[("LINE_ALLOWED_USERS", "LINE_ALLOWED_USERS=U123", "LINE_ALLOWED_USERS=U123,U456")])
    text = render(diff)
    assert "U123" in text
    assert "U456" in text


def test_no_changes_renders_plainly():
    assert render(Diff()) == "(no changes)"


def test_added_key_shown():
    diff = Diff(added=[("LINE_BASIC_ID", "LINE_BASIC_ID=@abc1234")])
    text = render(diff)
    assert "@abc1234" in text
    assert "(new)" in text
