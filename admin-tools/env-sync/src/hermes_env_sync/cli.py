"""hermes-env-sync CLI: diff, push, restart-only, list."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from hermes_env_sync import diff as diff_render
from hermes_env_sync import envfile
from hermes_env_sync import registry as registry_mod
from hermes_env_sync.restart import RestartVerificationError, restart_and_verify
from hermes_env_sync.ssh import REMOTE_ENV_PATH, REMOTE_ENV_TMP_PATH, SSHError, SSHTransport

BACKUP_DIR = Path(__file__).resolve().parents[2] / ".backups"


def _load_target(slug: str):
    return registry_mod.resolve_client(slug)


def _compute_diff(transport, target):
    remote_text = transport.read_file(REMOTE_ENV_PATH)
    local_text = target.env_path.read_text(encoding="utf-8")
    new_text, diff, warnings = envfile.upsert(remote_text, local_text)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return remote_text, new_text, diff


def cmd_diff(args: argparse.Namespace) -> int:
    target = _load_target(args.slug)
    transport = SSHTransport(ssh_target=target.ssh_target, ssh_key=target.ssh_key)
    _, _, diff = _compute_diff(transport, target)
    print(diff_render.render(diff))
    return 0


def _write_backup(slug: str, remote_text: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = BACKUP_DIR / f"{slug}-{stamp}.env"
    path.write_text(remote_text, encoding="utf-8")
    return path


def cmd_push(args: argparse.Namespace) -> int:
    target = _load_target(args.slug)
    transport = SSHTransport(ssh_target=target.ssh_target, ssh_key=target.ssh_key)

    remote_text, new_text, diff = _compute_diff(transport, target)
    print(diff_render.render(diff))

    if not diff.has_changes:
        print("nothing to change")
        return 0

    if args.dry_run:
        print("(dry run — no remote write, no restart)")
        return 0

    if not args.yes:
        answer = input("Apply these changes and restart the gateway? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    backup_path = _write_backup(args.slug, remote_text)
    print(f"backup saved to {backup_path}")

    try:
        transport.write_file_atomic(REMOTE_ENV_PATH, REMOTE_ENV_TMP_PATH, new_text)
    except SSHError as exc:
        print(f"error: remote write failed: {exc}", file=sys.stderr)
        return 1
    print("remote .env updated")

    try:
        new_pid = restart_and_verify(transport)
    except RestartVerificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"restart verified (pid={new_pid.pid}, start_time={new_pid.start_time})")
    return 0


def cmd_restart_only(args: argparse.Namespace) -> int:
    target = _load_target(args.slug)
    transport = SSHTransport(ssh_target=target.ssh_target, ssh_key=target.ssh_key)
    try:
        new_pid = restart_and_verify(transport)
    except RestartVerificationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"restart verified (pid={new_pid.pid}, start_time={new_pid.start_time})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    reg = registry_mod.load_registry()
    for slug in sorted(reg):
        print(slug)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-env-sync")
    sub = parser.add_subparsers(dest="command", required=True)

    p_diff = sub.add_parser("diff", help="show pending changes, read-only")
    p_diff.add_argument("slug")
    p_diff.set_defaults(func=cmd_diff)

    p_push = sub.add_parser("push", help="upsert env vars and restart+verify the gateway")
    p_push.add_argument("slug")
    p_push.add_argument("--dry-run", action="store_true")
    p_push.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_push.set_defaults(func=cmd_push)

    p_restart = sub.add_parser("restart-only", help="restart+verify without touching .env")
    p_restart.add_argument("slug")
    p_restart.set_defaults(func=cmd_restart_only)

    p_list = sub.add_parser("list", help="list known client slugs")
    p_list.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except registry_mod.RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SSHError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
