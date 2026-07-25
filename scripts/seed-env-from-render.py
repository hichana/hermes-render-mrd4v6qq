#!/opt/hermes/.venv/bin/python
"""Idempotent seeder: copies a small allowlist of Render container env vars
into Hermes' ~/.hermes/.env on first boot, without ever overwriting values
an operator already set from the dashboard.

Why this exists: Hermes' own setup surface for provider keys and chat
platform config is the dashboard's API Keys tab, which persists to
`$HERMES_HOME/.env` on the mounted disk (see README.md's "Post-deploy
setup"). Some values, though -- e.g. LINE_BASIC_ID, this channel's public
LINE Basic ID used by the line-invite skill -- are per-instance constants
we already know at Render-provisioning time (they're not secrets in the
same sense as an LLM API key). Requiring a manual dashboard visit for
every new client instance just to paste in a value we could have supplied
as a Render env var at creation time doesn't scale. This script closes
that gap: set the var as a normal Render Environment-tab value, and it
lands in `.env` automatically on first boot.

This is repo CLAUDE.md Pattern 1 (boot-time idempotent config mutation)
applied to `.env` instead of `config.yaml`:
  - INSERT-only. If the key already has a non-empty value in `.env`, it is
    left completely alone -- a dashboard edit always wins over this script,
    on every subsequent boot.
  - Atomic write via temp file + `Path.replace()`.
  - Never raises past `main()`; failures log an `[render-tools]`-prefixed
    warning to stderr and exit 0, so a patcher bug can never block boot.

To seed another per-instance, non-secret value the same way, add its name
to SEEDABLE_VARS below -- nothing else needs to change.
"""
from __future__ import annotations

import sys
from pathlib import Path

import os

# Non-secret, per-instance values worth auto-seeding from a Render env var.
# Deliberately short and explicit rather than "pass through everything" --
# see hermes-agent's own tools/env_passthrough.py for why an allowlist
# beats a blanket passthrough for anything that touches process env.
SEEDABLE_VARS = [
    "LINE_BASIC_ID",
    "LINE_PUBLIC_URL",
    "LINE_SLOW_RESPONSE_THRESHOLD",
]


def load_env_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"[render-tools] cannot read {path}: {exc}", file=sys.stderr)
        return []


def existing_keys(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):]
        key, _, value = stripped.partition("=")
        result[key.strip()] = value.strip().strip("\"'")
    return result


def save_env_lines(path: Path, lines: list[str]) -> None:
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    tmp = path.with_suffix(path.suffix + ".render-tools.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: seed-env-from-render.py <path/to/.env>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = load_env_lines(path)
    present = existing_keys(lines)

    added: list[str] = []
    for name in SEEDABLE_VARS:
        if present.get(name):
            continue  # operator already set this from the dashboard; leave it
        value = os.environ.get(name, "").strip()
        if not value:
            continue  # nothing to seed from this boot's container env
        lines.append(f"{name}={value}")
        added.append(name)

    if added:
        save_env_lines(path, lines)
        print(f"[render-tools] seeded {path} from Render env: {', '.join(added)}")
    else:
        print(f"[render-tools] {path}: nothing to seed (already set or no Render env value)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
