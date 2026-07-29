"""Append-only, version-controlled record of every remote write.

A `push` changes a live client instance, but leaves no trace in git: the
local client file it reads from lives under `clients/` (gitignored, real
secrets), and the remote `.env` it writes to isn't in any repo at all. The
`.backups/` snapshots are gitignored for the same reason. So the repo's
history has nothing to say about when a client instance last changed, or
what changed on it -- exactly the question you want answered when
something starts misbehaving in production.

This module writes one CSV row per remote-affecting run to a file that
*is* committed. It is deliberately a record of **which keys** moved, never
of any value: `build_record` reads key names off the Diff and nothing
else, so a secret cannot reach the file by construction rather than by
the author remembering not to log it. `test_auditlog.py` asserts that
directly.

Failures are logged too, with a non-`ok` outcome. A push whose remote
write landed but whose restart could not be verified is precisely the
state worth having a record of -- see the "restart requested is not proof
of a restart" warning in the repo's CLAUDE.md.
"""

from __future__ import annotations

import csv
import getpass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_PATH = Path(__file__).resolve().parents[2] / "push-log.csv"

CSV_COLUMNS = [
    "timestamp_utc",
    "operator",
    "slug",
    "action",
    "outcome",
    "keys_added",
    "keys_changed",
    "keys_removed",
    "gateway_pid",
    "gateway_start_time",
]

VALID_ACTIONS = frozenset({"push", "restart-only"})
VALID_OUTCOMES = frozenset({"ok", "write-failed", "restart-unverified"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _current_operator() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _join_keys(keys) -> str:
    return " ".join(sorted(keys))


def build_record(
    *,
    slug: str,
    action: str,
    outcome: str,
    diff: Any = None,
    gateway_pid: Any = None,
    timestamp: str | None = None,
    operator: str | None = None,
) -> dict[str, str]:
    """Build one CSV row. Key names only -- never a value."""
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown action {action!r}; expected one of {sorted(VALID_ACTIONS)}")
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}; expected one of {sorted(VALID_OUTCOMES)}")

    added = _join_keys(key for key, _ in diff.added) if diff is not None else ""
    changed = _join_keys(key for key, _, _ in diff.changed) if diff is not None else ""
    removed = _join_keys(diff.removed) if diff is not None else ""

    return {
        "timestamp_utc": timestamp or _utc_now_iso(),
        "operator": operator or _current_operator(),
        "slug": slug,
        "action": action,
        "outcome": outcome,
        "keys_added": added,
        "keys_changed": changed,
        "keys_removed": removed,
        "gateway_pid": str(gateway_pid.pid) if gateway_pid is not None else "",
        "gateway_start_time": str(gateway_pid.start_time) if gateway_pid is not None else "",
    }


def append_record(path: Path, record: dict[str, str]) -> None:
    """Append one row, writing the header first if the file is new or empty."""
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
        if needs_header:
            writer.writeheader()
        writer.writerow(record)
