"""Thin wrapper over the system `ssh` binary.

Deliberately not paramiko/fabric: this repo has zero Python-SSH-library
precedent, and the remote surface here is tiny (read one file, atomically
write one file, run a short command, read one file a couple more times).
Shelling out to `ssh` reuses the exact same binary, key, and host quirks
(e.g. the harmless "bad signature for ED25519 key" proxy warning) admins
already use for manual debugging per SERVICES.md.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

REMOTE_ENV_PATH = "/opt/data/.env"
REMOTE_ENV_TMP_PATH = "/opt/data/.env.hermes-env-sync.tmp"
REMOTE_PID_PATH = "/opt/data/gateway.pid"
SSH_CONNECT_TIMEOUT = 10
SSH_COMMAND_TIMEOUT = 30

# Render's SSH gateway occasionally drops a fresh connection outright
# ("Connection reset by peer") or a one-shot command intermittently fails
# for no reason tied to our command (observed live, 2026-07-27, on plain
# `cat` calls). Read-only commands are safe to retry blindly since they have
# no side effects; a couple of quick retries turns a flaky connection into
# a non-issue instead of aborting a whole push partway through.
READ_RETRY_ATTEMPTS = 3
READ_RETRY_DELAY_SECONDS = 2


class SSHError(RuntimeError):
    pass


class Transport(Protocol):
    """The remote operations env-sync needs. Real impl below; tests inject a fake."""

    def read_file(self, path: str) -> str: ...
    def write_file_atomic(self, path: str, tmp_path: str, content: str) -> None: ...
    def run_as_hermes(self, command: str) -> str: ...


@dataclass
class SSHTransport:
    ssh_target: str
    ssh_key: str

    def _ssh_argv(self, remote_command: str) -> list[str]:
        return [
            "ssh",
            "-i",
            self.ssh_key,
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
            self.ssh_target,
            remote_command,
        ]

    def _run(self, remote_command: str, *, input_text: str | None = None) -> str:
        try:
            proc = subprocess.run(
                self._ssh_argv(remote_command),
                input=input_text,
                capture_output=True,
                text=True,
                timeout=SSH_COMMAND_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise SSHError(f"ssh command timed out: {remote_command!r}") from exc
        # A stray host-key-signature warning on the proxy's connect banner is
        # expected and harmless (see SERVICES.md) — don't treat stderr output
        # alone as failure, only a non-zero exit.
        if proc.returncode != 0:
            raise SSHError(
                f"ssh command failed ({proc.returncode}): {remote_command!r}\n"
                f"stderr: {proc.stderr.strip()}"
            )
        return proc.stdout

    def read_file(self, path: str) -> str:
        # Retried: read-only and idempotent, so a flaky connection here just
        # costs a couple of seconds rather than aborting the whole push.
        last_error: SSHError | None = None
        for attempt in range(1, READ_RETRY_ATTEMPTS + 1):
            try:
                return self._run(f"cat {path}")
            except SSHError as exc:
                last_error = exc
                if attempt < READ_RETRY_ATTEMPTS:
                    time.sleep(READ_RETRY_DELAY_SECONDS)
        assert last_error is not None
        raise last_error

    def write_file_atomic(self, path: str, tmp_path: str, content: str) -> None:
        self._run(f"cat > {tmp_path}", input_text=content)
        self._run(f"mv {tmp_path} {path}")
        self._run(f"chown hermes:hermes {path}")

    def run_as_hermes(self, command: str) -> str:
        # Full path, not just `s6-setuidgid`: an interactive SSH login shell
        # doesn't get s6-overlay's /command PATH entry the way a
        # with-contenv cont-init hook does -- confirmed by hand against the
        # live instance (bare `s6-setuidgid` -> exit 127, not found).
        return self._run(f"/command/s6-setuidgid hermes {command}")
