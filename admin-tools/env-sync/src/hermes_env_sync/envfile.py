"""Targeted upsert of .env-format text.

Unlike the old boot-time seeder (insert-only, never overwrites), this is a
deliberate replace-if-present upsert: every key present in the local client
file always wins, on every run. That's the whole point — the seeder's
insert-only guarantee is exactly the silent-drift bug this tool exists to
fix. Any key NOT in the local file (e.g. something set by hand through
Hermes' own dashboard) is left completely untouched, in its original
position, byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawLine:
    text: str


@dataclass
class KVLine:
    key: str
    text: str


Line = RawLine | KVLine


@dataclass
class Diff:
    changed: list[tuple[str, str, str]] = field(default_factory=list)  # key, old_raw, new_raw
    added: list[tuple[str, str]] = field(default_factory=list)  # key, new_raw
    unchanged_count: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.changed or self.added)


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
        key = _parse_key(raw)
        if key is None:
            lines.append(RawLine(text=raw))
        else:
            lines.append(KVLine(key=key, text=raw))
    return lines


def _local_kv(local_lines: list[Line], warnings: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    seen: set[str] = set()
    for line in local_lines:
        if isinstance(line, KVLine):
            if line.key in seen:
                warnings.append(
                    f"key {line.key!r} appears more than once in the local "
                    "client file — last occurrence wins"
                )
            seen.add(line.key)
            result[line.key] = line.text
    return result


def upsert(remote_text: str, local_text: str) -> tuple[str, Diff, list[str]]:
    """Upsert `local_text`'s keys into `remote_text`.

    Returns (new_remote_text, diff, warnings). Every key present locally is
    forced to the local file's exact raw line, in the remote file's existing
    position if it already had that key, or appended (in local-file order)
    if it didn't. Every remote key absent from the local file is passed
    through completely unchanged, in its original position.
    """
    warnings: list[str] = []
    remote_lines = parse_lines(remote_text)
    local_lines = parse_lines(local_text)
    local_kv = _local_kv(local_lines, warnings)
    remaining = dict(local_kv)

    diff = Diff()
    out: list[str] = []

    for line in remote_lines:
        if isinstance(line, RawLine):
            out.append(line.text)
            continue
        if line.key in remaining:
            new_text = remaining.pop(line.key)
            if new_text != line.text:
                diff.changed.append((line.key, line.text, new_text))
            else:
                diff.unchanged_count += 1
            out.append(new_text)
        else:
            out.append(line.text)

    # Anything left in `remaining` was local-only — append in local-file order.
    for line in local_lines:
        if isinstance(line, KVLine) and line.key in remaining:
            new_text = remaining.pop(line.key)
            diff.added.append((line.key, new_text))
            out.append(new_text)

    new_text = "\n".join(out)
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, diff, warnings
