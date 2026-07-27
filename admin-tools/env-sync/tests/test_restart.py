import json

import pytest

from hermes_env_sync.restart import RestartVerificationError, restart_and_verify


class FakeTransport:
    def __init__(self, pid_sequence):
        self._pid_sequence = list(pid_sequence)
        self.commands = []

    def read_file(self, path):
        return json.dumps(self._pid_sequence[0] if len(self._pid_sequence) == 1
                           else self._pid_sequence.pop(0))

    def write_file_atomic(self, path, tmp_path, content):
        raise NotImplementedError

    def run_as_hermes(self, command):
        self.commands.append(command)
        return ""


def _fake_clock():
    t = [0.0]

    def now():
        return t[0]

    def sleep(seconds):
        t[0] += seconds

    return now, sleep


def test_restart_verified_when_pid_and_start_time_both_change():
    transport = FakeTransport([
        {"pid": 100, "kind": "hermes-gateway", "argv": [], "start_time": 111},
        {"pid": 200, "kind": "hermes-gateway", "argv": [], "start_time": 222},
    ])
    now, sleep = _fake_clock()
    result = restart_and_verify(transport, poll_interval=2, timeout=30, sleep=sleep, now=now)
    assert result.pid == 200
    assert result.start_time == 222
    assert transport.commands == ["kill -USR1 100"]


def test_restart_not_verified_if_pid_unchanged():
    transport = FakeTransport([
        {"pid": 100, "kind": "hermes-gateway", "argv": [], "start_time": 111},
    ])
    now, sleep = _fake_clock()
    with pytest.raises(RestartVerificationError):
        restart_and_verify(transport, poll_interval=2, timeout=6, sleep=sleep, now=now)


def test_restart_not_verified_if_only_pid_changes_start_time_same():
    # Guards the "both must differ" requirement -- a coincidental pid reuse
    # with the same start_time is NOT proof of a restart.
    class SamePidNewNumber:
        def __init__(self):
            self.calls = 0

        def read_file(self, path):
            self.calls += 1
            payload = {"pid": 100, "kind": "hermes-gateway", "argv": [], "start_time": 111}
            if self.calls > 1:
                payload["pid"] = 999  # pid changed...
                payload["start_time"] = 111  # ...but start_time didn't
            return json.dumps(payload)

        def write_file_atomic(self, *a, **k):
            raise NotImplementedError

        def run_as_hermes(self, command):
            return ""

    now, sleep = _fake_clock()
    with pytest.raises(RestartVerificationError):
        restart_and_verify(SamePidNewNumber(), poll_interval=2, timeout=6, sleep=sleep, now=now)
