"""Targeted upsert of .env-format text.

Unlike the old boot-time seeder (insert-only, never overwrites), this is a
deliberate replace-if-present upsert: every key present in the local client
file always wins, on every run. That's the whole point — the seeder's
insert-only guarantee is exactly the silent-drift bug this tool exists to
fix. Any key NOT MENTIONED AT ALL in the local file (e.g. something set by
hand through Hermes' own dashboard) is left completely untouched, in its
original position, byte-for-byte — deleting a line from the local file does
NOT delete it remotely, on purpose, since "never wrote an opinion on this
key" and "wrote an opinion that it shouldn't exist" are different things and
we can't tell them apart from a missing line alone.

To actually remove a key from the remote .env, say so explicitly with a
`!KEY_NAME` marker line (bang prefix, no `=`) instead of just deleting the
`KEY_NAME=...` line. See DeleteLine below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DELETE_MARKER_RE = re.compile(r"^!([A-Za-z_][A-Za-z0-9_]*)$")


@dataclass
class RawLine:
    text: str


@dataclass
class KVLine:
    key: str
    text: str


@dataclass
class DeleteLine:
    key: str
    text: str


Line = RawLine | KVLine | DeleteLine


@dataclass
class Diff:
    changed: list[tuple[str, str, str]] = field(default_factory=list)  # key, old_raw, new_raw
    added: list[tuple[str, str]] = field(default_factory=list)  # key, new_raw
    removed: list[str] = field(default_factory=list)  # key
    # Remote keys the local file says nothing about — neither `KEY=` nor
    # `!KEY`. Purely informational, and deliberately NOT part of has_changes:
    # leaving them alone is the upsert's entire contract. They're surfaced
    # because a one-way diff otherwise reports "(no changes)" whether the
    # remote holds the same keys as you or those plus a dozen you've never
    # tracked — which is how a live TELEGRAM_BOT_TOKEN went unrecorded for
    # weeks (2026-07-30). Names only; this list never carries values.
    untracked: list[str] = field(default_factory=list)  # key
    unchanged_count: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.changed or self.added or self.removed)


def _parse_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):]
    key, _, _ = stripped.partition("=")
    key = key.strip()
    return key or None


def parse_lines(text: str) -> list[Line]:
    lines: list[Line] = []
    for raw in text.splitlines():
        delete_match = _DELETE_MARKER_RE.match(raw.strip())
        if delete_match:
            lines.append(DeleteLine(key=delete_match.group(1), text=raw))
            continue
        key = _parse_key(raw)
        if key is None:
            lines.append(RawLine(text=raw))
        else:
            lines.append(KVLine(key=key, text=raw))
    return lines


def _local_directives(
    local_lines: list[Line], warnings: list[str]
) -> tuple[dict[str, str], set[str]]:
    """Split local lines into (keys to set/replace, keys to delete)."""
    set_kv: dict[str, str] = {}
    delete_keys: set[str] = set()
    seen: set[str] = set()
    for line in local_lines:
        if isinstance(line, KVLine):
            if line.key in seen:
                warnings.append(
                    f"key {line.key!r} appears more than once in the local "
                    "client file — last occurrence wins"
                )
            seen.add(line.key)
            set_kv[line.key] = line.text
            delete_keys.discard(line.key)
        elif isinstance(line, DeleteLine):
            if line.key in seen:
                warnings.append(
                    f"key {line.key!r} is both set and marked !{line.key} "
                    "for deletion in the local client file — deletion wins"
                )
            seen.add(line.key)
            delete_keys.add(line.key)
            set_kv.pop(line.key, None)
    return set_kv, delete_keys


def upsert(remote_text: str, local_text: str) -> tuple[str, Diff, list[str]]:
    """Upsert `local_text`'s keys into `remote_text`.

    Returns (new_remote_text, diff, warnings). Every `KEY=value` line
    present locally is forced to the local file's exact raw line, in the
    remote file's existing position if it already had that key, or appended
    (in local-file order) if it didn't. Every remote key absent from the
    local file is passed through completely unchanged, in its original
    position. A `!KEY_NAME` line locally removes that key's line from the
    remote file entirely, if present.
    """
    warnings: list[str] = []
    remote_lines = parse_lines(remote_text)
    local_lines = parse_lines(local_text)
    local_kv, delete_keys = _local_directives(local_lines, warnings)
    remaining = dict(local_kv)

    diff = Diff()
    out: list[str] = []

    for line in remote_lines:
        if isinstance(line, RawLine):
            out.append(line.text)
            continue
        if isinstance(line, DeleteLine):
            # A remote file should never itself contain a `!KEY` marker line
            # (those only mean something as a local directive) -- but if one
            # somehow exists, treat it as inert text rather than crashing.
            out.append(line.text)
            continue
        if line.key in delete_keys:
            diff.removed.append(line.key)
            continue  # drop this line entirely
        if line.key in remaining:
            new_text = remaining.pop(line.key)
            if new_text != line.text:
                diff.changed.append((line.key, line.text, new_text))
            else:
                diff.unchanged_count += 1
            out.append(new_text)
        else:
            # Not set locally and not marked for deletion: the local file has
            # no opinion on this key. Pass it through untouched, but record
            # that we saw it. Tested against `local_kv` rather than the
            # `remaining` dict, which has already been popped from by now.
            if line.key not in local_kv:
                diff.untracked.append(line.key)
            out.append(line.text)

    # Anything left in `remaining` was local-only — append in local-file order.
    for line in local_lines:
        if isinstance(line, KVLine) and line.key in remaining:
            new_text = remaining.pop(line.key)
            diff.added.append((line.key, new_text))
            out.append(new_text)

    # A !KEY marker for a key that was never present remotely is a no-op --
    # nothing to remove, but not an error either.

    new_text = "\n".join(out)
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, diff, warnings
