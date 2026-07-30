"""Tests for ``scripts/upgrade-preflight.sh``.

The script's whole job is to fetch the upstream files this repo reaches into
at two tags and report drift. Rather than mock that out, the fetch base is
parameterized (``PREFLIGHT_RAW_BASE``), so these tests point it at a local
``file://`` fixture tree and drive the real script end to end with no
network and no Docker.

Run with: ``python3 -m pytest tests/ -q``
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "upgrade-preflight.sh"

CURRENT = "vTEST.old"
CANDIDATE = "vTEST.new"


def manifest_paths() -> list[str]:
    """Every path in the live script's DEPS table.

    Read from the script rather than restated here: a fixture tree that lists
    the dependencies by hand goes stale the moment someone adds a manifest
    entry, and then every "0 blockers" test fails for a reason that has
    nothing to do with what it is testing. (Learned the hard way.)
    """
    text = SCRIPT.read_text()
    block = re.search(r"^DEPS=\((.*?)^\)", text, re.MULTILINE | re.DOTALL)
    assert block, "DEPS array not found in upgrade-preflight.sh"
    return [entry.split("|", 1)[0] for entry in re.findall(r'"([^"]+)"', block.group(1))]


# Content for the fixture files whose *contents* a check actually reads —
# the Dockerfile's shape assertions and every SYMBOLS pattern. Any other
# manifest path gets generic placeholder content below.
FIXTURE_CONTENT = {
    "Dockerfile": """\
FROM python:3.12
# s6-overlay is PID 1
RUN useradd -u 10000 -m -d /opt/data hermes
RUN cd ../ui-tui && npm run build
COPY --chmod=0755 docker/cont-init.d/02-reconcile-profiles /etc/cont-init.d/02-reconcile-profiles
ENV HERMES_HOME=/opt/data
ENTRYPOINT [ "/init", "/opt/hermes/docker/main-wrapper.sh" ]
CMD [ ]
""",
    "docker/main-wrapper.sh": "#!/bin/sh\nexec \"$@\"\n",
    "docker/s6-rc.d/user/contents.d/main-hermes": "",
    "docker/s6-rc.d/user/contents.d/dashboard": "",
    "docker/cont-init.d/015-supervise-perms": "#!/command/with-contenv sh\n",
    "docker/cont-init.d/02-reconcile-profiles": "#!/command/with-contenv sh\n",
    "hermes_constants.py": "def get_hermes_dir(new_subpath, old_name):\n    return None\n",
    "gateway/pairing.py": "class PairingStore:\n    def generate_code(self):\n        return None\n",
    "gateway/platforms/base.py": "    def enforces_own_access_policy(self):\n        return False\n",
    "gateway/run.py": """\
import signal
def request_restart(self, *, detached=False, via_service=False):
    return True
_PID_FILE = "gateway.pid"
_DRAIN = "restart_drain_timeout"
signal.SIGUSR1
""",
    "hermes_cli/container_boot.py": "SOUL_MD = 'SOUL.md'\n",
    "plugins/platforms/line/adapter.py": """\
def _allowed_for_source(source):
    return True
class _LineClient:
    async def get_profile(self, user_id):
        return {}
def _truthy_env(name, default=False):
    return default
class LineAdapter:
    async def _dispatch_event(self, event):
        return None
    async def _handle_message_event(self, event):
        return None
    async def _handle_postback_event(self, event):
        return None
""",
    "plugins/platforms/line/plugin.yaml": "name: line\nrequired_env:\n  - LINE_CHANNEL_SECRET\n",
    "cli-config.yaml.example": """\
# Session reset policy
session_reset:
  mode: both
  idle_minutes: 1440

skills:
  # external_dirs:
  #   - ~/.agents/skills
""",
}


def good_tree() -> dict:
    """A synthetic upstream tree that satisfies every check in the manifest.

    Derived from the live DEPS table, so adding a manifest entry never breaks
    these tests. Each test starts from this and breaks exactly one thing, so a
    failure names the check that caught it.
    """
    tree = {
        path: FIXTURE_CONTENT.get(path, f"# fixture placeholder for {path}\n")
        for path in manifest_paths()
    }
    # Files the script fetches or asserts on without a DEPS entry of their own.
    tree.update(FIXTURE_CONTENT)
    return tree


def write_tree(root: Path, tag: str, files: dict) -> None:
    for rel, content in files.items():
        path = root / tag / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


class PreflightCase(unittest.TestCase):
    """Base class: builds a two-tag fixture tree and runs the real script."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tags_dir = Path(self._tmp.name) / "tags"
        self.tags_dir.mkdir(parents=True)
        self.old = good_tree()
        self.new = good_tree()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_preflight(self, *args: str, cwd: Path | None = None):
        write_tree(self.tags_dir, CURRENT, self.old)
        write_tree(self.tags_dir, CANDIDATE, self.new)
        return subprocess.run(
            [str(SCRIPT), *args],
            cwd=str(cwd or REPO_ROOT),
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "PREFLIGHT_RAW_BASE": f"file://{self.tags_dir}",
            },
            capture_output=True,
            text=True,
        )

    def both_tags(self):
        return self.run_preflight(CANDIDATE, CURRENT)


class TestCleanUpgrade(PreflightCase):
    def test_identical_trees_pass_with_no_drift(self):
        result = self.both_tags()
        self.assertIn("PREFLIGHT: 0 blockers, 0 to review", result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)


class TestDriftSeverity(PreflightCase):
    def test_blocker_file_drift_fails_and_names_the_path(self):
        self.new["plugins/platforms/line/adapter.py"] += "\n# upstream edit\n"
        result = self.both_tags()
        self.assertIn("plugins/platforms/line/adapter.py", result.stdout)
        self.assertIn("PREFLIGHT: 1 blockers", result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_review_file_drift_passes_but_is_counted(self):
        self.new["gateway/run.py"] += "\n# upstream edit\n"
        result = self.both_tags()
        self.assertIn("gateway/run.py", result.stdout)
        self.assertIn("PREFLIGHT: 0 blockers, 1 to review", result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_manifest_file_at_candidate_is_a_blocker(self):
        del self.new["docker/s6-rc.d/user/contents.d/main-hermes"]
        result = self.both_tags()
        self.assertIn("docker/s6-rc.d/user/contents.d/main-hermes", result.stdout)
        self.assertIn("PREFLIGHT: 1 blockers", result.stdout)
        self.assertNotEqual(result.returncode, 0)


class TestSymbolManifest(PreflightCase):
    def test_lost_symbol_is_a_blocker_even_though_the_file_exists(self):
        # File still there, still diffs as a "review" file, but the symbol our
        # patch imports by name is gone.
        self.new["gateway/platforms/base.py"] = "    def some_other_thing(self):\n        return False\n"
        result = self.both_tags()
        self.assertIn("enforces_own_access_policy", result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_env_sync_restart_path_symbols_are_checked(self):
        self.new["gateway/run.py"] = "# rewritten upstream, no restart plumbing\n"
        result = self.both_tags()
        self.assertIn("request_restart", result.stdout)
        self.assertIn("SIGUSR1", result.stdout)
        self.assertNotEqual(result.returncode, 0)


class TestStructuralAssertions(PreflightCase):
    def test_changed_entrypoint_is_a_blocker(self):
        self.new["Dockerfile"] = self.new["Dockerfile"].replace(
            'ENTRYPOINT [ "/init", "/opt/hermes/docker/main-wrapper.sh" ]',
            'ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/hermes/docker/entrypoint.sh"]',
        )
        result = self.both_tags()
        self.assertIn("ENTRYPOINT", result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_dropped_s6_overlay_is_a_blocker(self):
        self.new["Dockerfile"] = self.new["Dockerfile"].replace(
            "# s6-overlay is PID 1", "# tini is PID 1"
        )
        result = self.both_tags()
        self.assertIn("s6-overlay", result.stdout)
        self.assertNotEqual(result.returncode, 0)


class TestTagResolution(PreflightCase):
    def test_missing_candidate_tag_fails_loudly_with_no_all_clear(self):
        result = self.run_preflight("vTEST.nonexistent", CURRENT)
        self.assertIn("not found", result.stdout + result.stderr)
        self.assertNotIn("PREFLIGHT: 0 blockers", result.stdout)
        self.assertNotEqual(result.returncode, 0)

    def test_current_tag_defaults_to_the_dockerfile_pin(self):
        with TemporaryDirectory() as fake_repo_dir:
            fake_repo = Path(fake_repo_dir)
            (fake_repo / "Dockerfile").write_text(
                f"ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent:{CURRENT}\n"
            )
            (fake_repo / "scripts").mkdir()
            result = self.run_preflight(CANDIDATE, cwd=fake_repo)
        self.assertIn(CURRENT, result.stdout)
        self.assertIn("PREFLIGHT: 0 blockers, 0 to review", result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)


class TestConfigDefaultDrift(PreflightCase):
    def test_changed_default_is_reported_with_its_key(self):
        self.new["cli-config.yaml.example"] = self.new[
            "cli-config.yaml.example"
        ].replace("mode: both", "mode: none")
        result = self.both_tags()
        self.assertIn("mode: none", result.stdout)

    def test_comment_only_change_is_not_reported_as_a_default_change(self):
        self.new["cli-config.yaml.example"] = self.new[
            "cli-config.yaml.example"
        ].replace("# Session reset policy", "# Session reset policy (rewritten prose)")
        result = self.both_tags()
        self.assertNotIn("CONFIG DEFAULT", result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
