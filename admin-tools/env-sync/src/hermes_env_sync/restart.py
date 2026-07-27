"""Trigger a gateway restart and verify it actually happened.

Per this repo's CLAUDE.md ("Env-var allowlist edits vs. pairing-store
approvals"): never trust a "restart requested" exit code or log line as
proof a restart happened. The only ground truth is /opt/data/gateway.pid's
(pid, start_time) pair changing.

Does NOT use `hermes gateway restart` -- confirmed live (2026-07-27) that
it's a silent no-op on this deployment shape: the gateway is the
container's bare main process (started by main-wrapper.sh, never through
`hermes gateway install`), so `hermes gateway status` reports "Running
manually, not as a system service" and the CLI's restart subcommand has no
registered service to dispatch to. It exits 0 with zero output and changes
nothing. The dashboard's own `POST /api/gateway/restart` shells out to this
identical subcommand, so it shares the same bug -- not a usable
alternative either.

What DOES work, confirmed live: sending SIGUSR1 directly to the gateway's
own pid, as the `hermes` user specifically. Root cannot do this (EPERM --
Render's runtime drops CAP_KILL, and kill() requires either a UID match or
that capability), but self-signaling as the owning user is always
permitted regardless of capabilities. gateway/run.py wires SIGUSR1 to
`request_restart(via_service=True)`, which drains in-flight agent runs (up
to the configurable `agent.restart_drain_timeout`, default 180s) then
exits; the container's own s6 supervision (confirmed present as
`s6-supervise main-hermes` in the process tree) relaunches it, which is
what actually produces a new pid. In practice, on an idle instance, this
completed in under 20s -- nowhere near the 180s ceiling -- but the poll
timeout below is set well past that ceiling anyway, since drain time
scales with in-flight work.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from hermes_env_sync.ssh import REMOTE_PID_PATH, Transport

POLL_INTERVAL_SECONDS = 3
POLL_TIMEOUT_SECONDS = 200


class RestartVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GatewayPid:
    pid: int
    start_time: int


def read_gateway_pid(transport: Transport) -> GatewayPid:
    raw = transport.read_file(REMOTE_PID_PATH)
    try:
        data = json.loads(raw)
        return GatewayPid(pid=int(data["pid"]), start_time=int(data["start_time"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise RestartVerificationError(
            f"could not parse {REMOTE_PID_PATH} — no baseline to verify a "
            f"restart against: {exc}"
        ) from exc


def restart_and_verify(
    transport: Transport,
    *,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    timeout: float = POLL_TIMEOUT_SECONDS,
    sleep=time.sleep,
    now=time.monotonic,
) -> GatewayPid:
    """Trigger `hermes gateway restart` and poll gateway.pid until it changes.

    Returns the new GatewayPid on success. Raises RestartVerificationError
    on timeout — callers must treat that as a failed restart, not a warning.
    """
    before = read_gateway_pid(transport)

    # Send SIGUSR1 directly to the pid we just read, as the `hermes` user --
    # see module docstring for why (not `hermes gateway restart`, and not
    # root). Exit code / output of this command is deliberately not
    # inspected for success either way; only the pid/start_time diff below
    # counts as proof.
    transport.run_as_hermes(f"kill -USR1 {before.pid}")

    deadline = now() + timeout
    last_seen = before
    while now() < deadline:
        sleep(poll_interval)
        try:
            last_seen = read_gateway_pid(transport)
        except RestartVerificationError:
            continue
        if last_seen.pid != before.pid and last_seen.start_time != before.start_time:
            return last_seen

    raise RestartVerificationError(
        f"restart not verified within {timeout}s — gateway.pid still shows "
        f"pid={last_seen.pid} start_time={last_seen.start_time} "
        f"(baseline was pid={before.pid} start_time={before.start_time}). "
        "Check /opt/data/logs/gateway.log by hand before assuming the "
        "restart happened."
    )
