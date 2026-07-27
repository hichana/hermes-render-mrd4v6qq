"""Render an envfile.Diff for interactive confirmation.

Secret-shaped keys (*_TOKEN, *_KEY, *_SECRET, *_PASSWORD) are redacted to a
length + short hash fingerprint — enough to confirm "yes, this changed" and
catch an obvious mistake (e.g. pasted the wrong client's key, one accidental
character) without ever printing the actual secret to a terminal/scrollback.
Everything else (LINE_BASIC_ID, LINE_ALLOWED_USERS, LINE_PUBLIC_URL, etc.) is
shown in full — an admin needs to actually read allowlist/config values to
verify they're correct, not just that they changed.
"""

from __future__ import annotations

import hashlib

from hermes_env_sync.envfile import Diff

_SECRET_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD")


def _is_secret(key: str) -> bool:
    return any(key.endswith(suffix) for suffix in _SECRET_SUFFIXES)


def _value_of(raw_line: str) -> str:
    _, _, value = raw_line.partition("=")
    return value.strip().strip("\"'")


def _fingerprint(value: str) -> str:
    if not value:
        return "(empty)"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:6]
    return f"len={len(value)} sha256={digest}"


def render(diff: Diff) -> str:
    lines: list[str] = []
    for key, old_raw, new_raw in diff.changed:
        if _is_secret(key):
            lines.append(
                f"  ~ {key}: {_fingerprint(_value_of(old_raw))} -> "
                f"{_fingerprint(_value_of(new_raw))}"
            )
        else:
            lines.append(f"  ~ {key}: {_value_of(old_raw)!r} -> {_value_of(new_raw)!r}")
    for key, new_raw in diff.added:
        if _is_secret(key):
            lines.append(f"  + {key}: {_fingerprint(_value_of(new_raw))} (new)")
        else:
            lines.append(f"  + {key}: {_value_of(new_raw)!r} (new)")
    if not lines:
        return "(no changes)"
    lines.append(f"  ({diff.unchanged_count} other key(s) unchanged)")
    return "\n".join(lines)
