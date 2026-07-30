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


def test_deleting_local_line_does_not_remove_it_remotely():
    # The gotcha this test guards: just removing a KEY=value line from the
    # local file must NOT delete it remotely -- that's ambiguous with
    # "never had an opinion on this key" (e.g. dashboard-managed values).
    remote = "FOO=old\nPOOP=poop\n"
    local = "FOO=old\n"
    new_text, diff, _ = upsert(remote, local)
    assert "POOP=poop" in new_text
    assert not diff.has_changes


def test_bang_marker_removes_key_remotely():
    remote = "FOO=old\nPOOP=poop\n"
    local = "FOO=old\n!POOP\n"
    new_text, diff, _ = upsert(remote, local)
    assert "POOP" not in new_text
    assert diff.removed == ["POOP"]
    assert diff.has_changes


def test_bang_marker_for_absent_key_is_a_noop():
    remote = "FOO=old\n"
    local = "FOO=old\n!NEVER_EXISTED\n"
    new_text, diff, _ = upsert(remote, local)
    assert new_text == remote
    assert diff.removed == []
    assert not diff.has_changes


def test_bang_marker_wins_over_a_set_for_same_key():
    remote = "FOO=old\n"
    local = "FOO=new\n!FOO\n"
    new_text, diff, warnings = upsert(remote, local)
    assert "FOO" not in new_text
    assert diff.removed == ["FOO"]
    assert diff.changed == []
    assert any("FOO" in w for w in warnings)


# --- untracked remote keys -------------------------------------------------
#
# The upsert is deliberately one-way, which makes a clean diff mean "nothing
# I track drifted" rather than "the files match". Reporting remote-only keys
# closes that read-side blind spot; it must not change any write behavior.
# See admin-tools/env-sync/README.md and ARCHITECTURE.md.


def test_remote_only_keys_are_reported_as_untracked():
    remote = "FOO=old\nTELEGRAM_BOT_TOKEN=secret\nBROWSER_SESSION_TIMEOUT=60\n"
    local = "FOO=old\n"
    _, diff, _ = upsert(remote, local)
    assert diff.untracked == ["TELEGRAM_BOT_TOKEN", "BROWSER_SESSION_TIMEOUT"]


def test_untracked_keys_are_not_changes():
    """The whole point: informational only. `push` must still say nothing to do."""
    remote = "FOO=old\nREMOTE_ONLY=x\n"
    local = "FOO=old\n"
    new_text, diff, _ = upsert(remote, local)
    assert diff.untracked == ["REMOTE_ONLY"]
    assert diff.has_changes is False
    assert new_text == remote  # byte-identical: nothing written


def test_locally_set_keys_are_never_untracked():
    remote = "FOO=old\nBAR=old\n"
    local = "FOO=new\nBAR=old\n"
    _, diff, _ = upsert(remote, local)
    assert diff.untracked == []


def test_bang_marked_key_is_mentioned_locally_so_not_untracked():
    remote = "FOO=old\nGONE=x\n"
    local = "FOO=old\n!GONE\n"
    _, diff, _ = upsert(remote, local)
    assert diff.removed == ["GONE"]
    assert diff.untracked == []


def test_comments_and_blank_lines_are_not_untracked_keys():
    remote = "# a comment\n\nFOO=old\n"
    local = "FOO=old\n"
    _, diff, _ = upsert(remote, local)
    assert diff.untracked == []
