"""Load and validate clients/registry.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_REGISTRY_PATH = _REPO_ROOT / "clients" / "registry.yaml"
DEFAULT_CLIENTS_DIR = _REPO_ROOT / "clients"


class RegistryError(Exception):
    pass


@dataclass
class ClientTarget:
    slug: str
    ssh_target: str
    ssh_key: str
    env_path: Path


def load_registry(
    registry_path: Path = DEFAULT_REGISTRY_PATH,
    clients_dir: Path = DEFAULT_CLIENTS_DIR,
) -> dict[str, ClientTarget]:
    if not registry_path.exists():
        raise RegistryError(
            f"no registry file at {registry_path} — copy "
            "admin-tools/env-sync/registry.yaml.example to clients/registry.yaml "
            "and fill in your clients"
        )
    try:
        data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RegistryError(f"could not parse {registry_path}: {exc}") from exc

    clients = data.get("clients") or {}
    if not isinstance(clients, dict):
        raise RegistryError(f"{registry_path}: 'clients' must be a mapping")

    default_key = data.get("default_ssh_key", "~/.ssh/render_hermes")

    result: dict[str, ClientTarget] = {}
    for slug, entry in clients.items():
        if not isinstance(entry, dict) or not entry.get("ssh_target"):
            raise RegistryError(f"{registry_path}: client {slug!r} is missing ssh_target")
        result[slug] = ClientTarget(
            slug=slug,
            ssh_target=entry["ssh_target"],
            ssh_key=entry.get("ssh_key", default_key),
            env_path=clients_dir / f"{slug}.env",
        )
    return result


def resolve_client(slug: str, **kwargs) -> ClientTarget:
    registry = load_registry(**kwargs)
    if slug not in registry:
        known = ", ".join(sorted(registry)) or "(none)"
        raise RegistryError(f"unknown client {slug!r}. Known clients: {known}")
    target = registry[slug]
    if not target.env_path.exists():
        raise RegistryError(
            f"no local env file at {target.env_path} — copy "
            "admin-tools/env-sync/client.env.example there and fill in values"
        )
    return target
