from hermes_env_sync.envfile import upsert


def test_changed_key_replaced_raw_line_preserved_elsewhere():
    remote = "FOO=old\nBAR=keep-me\n"
    local = "FOO=new\n"
    new_text, diff, warnings = upsert(remote, local)
    assert new_text == "FOO=new\nBAR=keep-me\n"
    assert diff.changed == [("FOO", "FOO=old", "FOO=new")]
    assert diff.added == []
    assert warnings == []


def test_local_only_key_appended_in_order():
    remote = "FOO=old\n"
    local = "FOO=old\nNEW1=a\nNEW2=b\n"
    new_text, diff, _ = upsert(remote, local)
    assert new_text == "FOO=old\nNEW1=a\nNEW2=b\n"
    assert diff.changed == []
    assert diff.added == [("NEW1", "NEW1=a"), ("NEW2", "NEW2=b")]
    assert diff.unchanged_count == 1


def test_remote_only_key_untouched():
    remote = "# comment\nFOO=old\nDASHBOARD_SET_KEY=some-value\n"
    local = "FOO=new\n"
    new_text, diff, _ = upsert(remote, local)
    assert "DASHBOARD_SET_KEY=some-value" in new_text
    assert "# comment" in new_text
    assert new_text.splitlines() == [
        "# comment",
        "FOO=new",
        "DASHBOARD_SET_KEY=some-value",
    ]
    assert diff.changed == [("FOO", "FOO=old", "FOO=new")]


def test_comments_and_blank_lines_preserved_in_position():
    remote = "\n# header\nFOO=old\n\n# footer\n"
    local = "FOO=new\n"
    new_text, _, _ = upsert(remote, local)
    assert new_text.splitlines() == ["", "# header", "FOO=new", "", "# footer"]


def test_duplicate_local_key_warns_last_wins():
    remote = "FOO=old\n"
    local = "FOO=first\nFOO=second\n"
    new_text, diff, warnings = upsert(remote, local)
    assert "FOO=second" in new_text
    assert diff.changed == [("FOO", "FOO=old", "FOO=second")]
    assert any("FOO" in w for w in warnings)


def test_no_changes_reports_unchanged():
    remote = "FOO=same\n"
    local = "FOO=same\n"
    new_text, diff, _ = upsert(remote, local)
    assert new_text == remote
    assert not diff.has_changes
    assert diff.unchanged_count == 1


def test_quoting_style_preserved_from_local_raw_line():
    remote = 'FOO=old\n'
    local = 'FOO="quoted value"\n'
    new_text, diff, _ = upsert(remote, local)
    assert 'FOO="quoted value"' in new_text
    assert diff.changed == [("FOO", "FOO=old", 'FOO="quoted value"')]
