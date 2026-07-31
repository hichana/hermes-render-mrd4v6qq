#!/opt/hermes/.venv/bin/python
"""Idempotent patcher for Hermes' ~/.hermes/config.yaml on Render.

Adds things the first time it runs against a given config.yaml:

  skills.external_dirs -- exposes this repo's `skills/` bundle (baked into
  the image at /opt/render-tools/skills-local) to skills_list() and the /
  slash command surface, without colliding with the upstream skills_sync
  flow on /opt/data/skills. Currently that bundle is just `line-invite`
  (plans/line-dm-pairing-plan.md Phase 6c) — the manager-initiated LINE
  join-invite QR flow.

  gateway.multiplex_profiles -- enables per-profile gateway processes,
  allowing multiple agents (each with its own port and configuration) to
  run on the same container. Declared in render.yaml; synced here into
  the live config.

The patcher is INSERT-only by design (repo CLAUDE.md Pattern 1). If the
key already exists (even pointing somewhere different), it leaves it
alone. This means:
  - Re-running the patcher on every boot is safe.
  - Users who edit config.yaml from the dashboard own those edits.

Uses PyYAML, which ships with Hermes' .venv.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

RENDER_SKILL_DIR = "/opt/render-tools/skills-local"


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[render-tools] cannot read {path}: {exc}", file=sys.stderr)
        return {}
    if not raw.strip():
        return {}
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        print(
            f"[render-tools] {path} is not valid YAML ({exc}); refusing to patch",
            file=sys.stderr,
        )
        sys.exit(0)
    return data if isinstance(data, dict) else {}


def ensure_external_skill_dir(config: dict) -> bool:
    """Append RENDER_SKILL_DIR to skills.external_dirs if missing. Returns True if changed."""
    skills = config.setdefault("skills", {})
    if not isinstance(skills, dict):
        print(
            "[render-tools] skills is not a mapping; skipping external_dirs",
            file=sys.stderr,
        )
        return False
    existing = skills.get("external_dirs")
    if existing is None:
        skills["external_dirs"] = [RENDER_SKILL_DIR]
        return True
    if not isinstance(existing, list):
        print(
            "[render-tools] skills.external_dirs is not a list; skipping",
            file=sys.stderr,
        )
        return False
    if RENDER_SKILL_DIR in existing:
        return False
    existing.append(RENDER_SKILL_DIR)
    return True


def ensure_gateway_multiplex(config: dict) -> bool:
    """Set gateway.multiplex_profiles if missing. Returns True if changed."""
    gateway = config.setdefault("gateway", {})
    if not isinstance(gateway, dict):
        print(
            "[render-tools] gateway is not a mapping; skipping multiplex_profiles",
            file=sys.stderr,
        )
        return False
    if "multiplex_profiles" in gateway:
        return False
    gateway["multiplex_profiles"] = True
    return True


def save_config(path: Path, config: dict) -> None:
    text = yaml.safe_dump(
        config,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    tmp = path.with_suffix(path.suffix + ".render-tools.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch-config.py <path/to/config.yaml>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    path.parent.mkdir(parents=True, exist_ok=True)
    config = load_config(path)
    skill_changed = ensure_external_skill_dir(config)
    gateway_changed = ensure_gateway_multiplex(config)
    if skill_changed or gateway_changed:
        save_config(path, config)
        patches = []
        if skill_changed:
            patches.append(f"skills.external_dirs += {RENDER_SKILL_DIR}")
        if gateway_changed:
            patches.append("gateway.multiplex_profiles = true")
        print(f"[render-tools] patched {path}: {', '.join(patches)}")
    else:
        print(f"[render-tools] {path} already up to date; nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
