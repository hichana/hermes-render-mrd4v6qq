import pytest

from hermes_env_sync.registry import RegistryError, load_registry, resolve_client


def test_load_registry(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        "default_ssh_key: ~/.ssh/default\n"
        "clients:\n"
        "  acme:\n"
        "    ssh_target: srv-acme@ssh.oregon.render.com\n"
        "  other:\n"
        "    ssh_target: srv-other@ssh.oregon.render.com\n"
        "    ssh_key: ~/.ssh/other\n"
    )
    clients_dir = tmp_path / "clients"
    reg = load_registry(registry_path=registry_path, clients_dir=clients_dir)
    assert reg["acme"].ssh_key == "~/.ssh/default"
    assert reg["other"].ssh_key == "~/.ssh/other"
    assert reg["acme"].env_path == clients_dir / "acme.env"


def test_missing_registry_file_raises(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(registry_path=tmp_path / "nope.yaml", clients_dir=tmp_path)


def test_client_missing_ssh_target_raises(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text("clients:\n  bad:\n    ssh_key: ~/.ssh/x\n")
    with pytest.raises(RegistryError):
        load_registry(registry_path=registry_path, clients_dir=tmp_path)


def test_resolve_client_requires_env_file(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        "clients:\n  acme:\n    ssh_target: srv-acme@ssh.oregon.render.com\n"
    )
    clients_dir = tmp_path / "clients"
    clients_dir.mkdir()
    with pytest.raises(RegistryError):
        resolve_client("acme", registry_path=registry_path, clients_dir=clients_dir)

    (clients_dir / "acme.env").write_text("FOO=bar\n")
    target = resolve_client("acme", registry_path=registry_path, clients_dir=clients_dir)
    assert target.slug == "acme"


def test_resolve_unknown_client_raises(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    registry_path.write_text(
        "clients:\n  acme:\n    ssh_target: srv-acme@ssh.oregon.render.com\n"
    )
    with pytest.raises(RegistryError):
        resolve_client("nope", registry_path=registry_path, clients_dir=tmp_path)
