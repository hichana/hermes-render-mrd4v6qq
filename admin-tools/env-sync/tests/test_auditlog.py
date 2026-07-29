import csv

import pytest

from hermes_env_sync import auditlog
from hermes_env_sync.diff import Diff
from hermes_env_sync.restart import GatewayPid


def _diff_with_secrets():
    diff = Diff()
    diff.changed.append(
        ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_ACCESS_TOKEN=old-secret",
         "LINE_CHANNEL_ACCESS_TOKEN=new-secret")
    )
    diff.added.append(("LINE_ALLOWED_GROUPS", "LINE_ALLOWED_GROUPS=Cabc123"))
    diff.removed.append("TELEGRAM_HOME_CHANNEL")
    return diff


def test_build_record_lists_only_key_names():
    record = auditlog.build_record(
        slug="ngraph-main",
        action="push",
        outcome="ok",
        diff=_diff_with_secrets(),
        timestamp="2026-07-29T12:00:00Z",
        operator="matt",
    )
    assert record["keys_changed"] == "LINE_CHANNEL_ACCESS_TOKEN"
    assert record["keys_added"] == "LINE_ALLOWED_GROUPS"
    assert record["keys_removed"] == "TELEGRAM_HOME_CHANNEL"


def test_build_record_never_contains_a_value():
    # The whole reason this file is safe to commit: key names only, never the
    # right-hand side of any `KEY=value` line.
    record = auditlog.build_record(
        slug="ngraph-main",
        action="push",
        outcome="ok",
        diff=_diff_with_secrets(),
        timestamp="2026-07-29T12:00:00Z",
        operator="matt",
    )
    blob = " ".join(record.values())
    for secret in ("old-secret", "new-secret", "Cabc123", "="):
        assert secret not in blob


def test_build_record_sorts_keys_for_stable_diffs():
    diff = Diff()
    diff.added.append(("ZULU", "ZULU=1"))
    diff.added.append(("ALPHA", "ALPHA=1"))
    record = auditlog.build_record(
        slug="c", action="push", outcome="ok", diff=diff,
        timestamp="t", operator="o",
    )
    assert record["keys_added"] == "ALPHA ZULU"


def test_build_record_carries_gateway_pid():
    record = auditlog.build_record(
        slug="c", action="push", outcome="ok", diff=Diff(),
        gateway_pid=GatewayPid(pid=5784, start_time=99),
        timestamp="t", operator="o",
    )
    assert record["gateway_pid"] == "5784"
    assert record["gateway_start_time"] == "99"


def test_build_record_without_diff_or_pid_leaves_fields_empty():
    record = auditlog.build_record(
        slug="c", action="restart-only", outcome="ok",
        timestamp="t", operator="o",
    )
    assert record["keys_added"] == ""
    assert record["keys_changed"] == ""
    assert record["keys_removed"] == ""
    assert record["gateway_pid"] == ""
    assert set(record) == set(auditlog.CSV_COLUMNS)


def test_append_record_writes_header_once(tmp_path):
    path = tmp_path / "push-log.csv"
    record = auditlog.build_record(
        slug="c", action="push", outcome="ok", diff=Diff(),
        timestamp="t", operator="o",
    )
    auditlog.append_record(path, record)
    auditlog.append_record(path, record)

    rows = path.read_text(encoding="utf-8").splitlines()
    assert rows[0] == ",".join(auditlog.CSV_COLUMNS)
    assert len(rows) == 3  # header + two records


def test_append_record_appends_to_an_existing_log(tmp_path):
    path = tmp_path / "push-log.csv"
    path.write_text(
        ",".join(auditlog.CSV_COLUMNS) + "\n"
        "2026-01-01T00:00:00Z,o,old,push,ok,,,,,\n",
        encoding="utf-8",
    )
    auditlog.append_record(
        path,
        auditlog.build_record(
            slug="new", action="push", outcome="ok", diff=Diff(),
            timestamp="t", operator="o",
        ),
    )
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["slug"] for row in rows] == ["old", "new"]


def test_append_record_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "push-log.csv"
    auditlog.append_record(
        path,
        auditlog.build_record(
            slug="c", action="push", outcome="ok", diff=Diff(),
            timestamp="t", operator="o",
        ),
    )
    assert path.exists()


def test_append_record_round_trips_through_csv_reader(tmp_path):
    path = tmp_path / "push-log.csv"
    record = auditlog.build_record(
        slug="ngraph-main", action="push", outcome="ok",
        diff=_diff_with_secrets(), gateway_pid=GatewayPid(pid=1, start_time=2),
        timestamp="2026-07-29T12:00:00Z", operator="matt",
    )
    auditlog.append_record(path, record)
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [record]


def test_record_failure_is_recorded_like_any_other_outcome(tmp_path):
    path = tmp_path / "push-log.csv"
    auditlog.append_record(
        path,
        auditlog.build_record(
            slug="c", action="push", outcome="restart-unverified",
            diff=_diff_with_secrets(), timestamp="t", operator="o",
        ),
    )
    with path.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    # A push whose restart could not be verified still wrote to the remote
    # .env -- the record must show that, not just successes.
    assert row["outcome"] == "restart-unverified"
    assert row["keys_changed"] == "LINE_CHANNEL_ACCESS_TOKEN"


def test_timestamp_defaults_to_utc_iso8601():
    record = auditlog.build_record(slug="c", action="push", outcome="ok")
    assert record["timestamp_utc"].endswith("Z")
    assert len(record["timestamp_utc"]) == len("2026-07-29T12:00:00Z")


def test_operator_defaults_to_something_nonempty():
    record = auditlog.build_record(slug="c", action="push", outcome="ok")
    assert record["operator"]


def test_unknown_action_is_rejected():
    with pytest.raises(ValueError):
        auditlog.build_record(slug="c", action="nonsense", outcome="ok")


def test_unknown_outcome_is_rejected():
    with pytest.raises(ValueError):
        auditlog.build_record(slug="c", action="push", outcome="nonsense")
