"""Coverage tests for ``scripts/upgrade-preflight.sh``'s manifest.

The preflight script is a closed-world check: it verifies the upstream
dependencies listed in its ``DEPS`` / ``SYMBOLS`` tables and is blind to
everything else. That makes an unregistered dependency the script's worst
failure mode — it reports a confident all-clear for a bump that then breaks
something nobody was watching.

This file closes the mechanically-detectable half of that gap. The repo
already declares its upstream touchpoints in machine-readable places:

  * every ``+++ b/<path>`` header in ``patches/*.patch`` is, by definition, a
    file we depend on the exact shape of
  * every ``COPY ... /opt/hermes/...`` line in the ``Dockerfile`` names an
    upstream directory we write into
  * every ``from <upstream-module> import <symbol>`` in our own sources (and
    in the patches' added lines) names a module and a symbol by name

So these are derived, not maintained: add a dependency without registering it
and a test goes red, rather than the next bump quietly not checking it.

What this CANNOT catch: a new upstream mechanism we have not started depending
on yet. Nothing can. That is what UPGRADING.md Phase 0's release-notes read is
for.

Run with: ``python3 -m pytest tests/ -q``
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO_ROOT / "scripts" / "upgrade-preflight.sh"
DOCKERFILE = REPO_ROOT / "Dockerfile"
PATCH_DIR = REPO_ROOT / "patches"

# Top-level upstream module names, i.e. what an import has to start with for us
# to consider it a dependency on Hermes' own source rather than on the stdlib
# or a third-party package.
UPSTREAM_ROOTS = {
    "agent",
    "gateway",
    "hermes_cli",
    "hermes_constants",
    "hermes_logging",
    "hermes_state",
    "hermes_time",
    "model_tools",
    "plugins",
    "providers",
    "toolsets",
    "tools",
    "utils",
}

# Our own Python that runs inside the container against Hermes' source tree.
TRACKED_SOURCE_GLOBS = ("modules/**/*.py", "skills/**/*.py", "scripts/*.py")


# --- parsing the manifest out of the shell script -----------------------


def _bash_array(name: str, text: str) -> list[str]:
    """Extract the quoted entries of a ``NAME=( "a" "b" )`` bash array."""
    match = re.search(rf"^{name}=\((.*?)^\)", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name} array not found in {PREFLIGHT}"
    return re.findall(r'"([^"]+)"', match.group(1))


class Manifest:
    def __init__(self) -> None:
        text = PREFLIGHT.read_text()
        self.deps: dict[str, str] = {}
        for entry in _bash_array("DEPS", text):
            path, severity, _note = entry.split("|", 2)
            self.deps[path] = severity
        self.symbol_patterns: list[tuple[str, str]] = []
        for entry in _bash_array("SYMBOLS", text):
            path, pattern, _label = entry.split("|", 2)
            self.symbol_patterns.append((path, pattern))

    def covers_symbol(self, symbol: str) -> bool:
        """True if any SYMBOLS pattern mentions this symbol name."""
        return any(symbol in pattern for _path, pattern in self.symbol_patterns)

    def covers_directory(self, directory: str) -> bool:
        """True if any DEPS entry lives in this upstream directory."""
        return any(str(Path(p).parent) == directory for p in self.deps)


# --- parsing the repo's own declarations --------------------------------


def patch_files() -> list[Path]:
    return sorted(PATCH_DIR.glob("*.patch"))


def applied_patches() -> set[str]:
    """Patch filenames the Dockerfile actually `git apply`s into the image."""
    text = DOCKERFILE.read_text()
    apply_step = "\n".join(
        line for line in text.splitlines() if "git apply" in line
    )
    return {p.name for p in patch_files() if p.name in apply_step}


def patch_targets(patch: Path) -> set[str]:
    return set(re.findall(r"^\+\+\+ b/(\S+)", patch.read_text(), re.MULTILINE))


def patch_added_lines(patch: Path) -> list[str]:
    return [
        line[1:]
        for line in patch.read_text().splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def copy_destinations() -> set[str]:
    """Upstream-relative paths this repo COPYs into /opt/hermes."""
    dests = set()
    for line in DOCKERFILE.read_text().splitlines():
        for dest in re.findall(r"/opt/hermes/(\S+)", line):
            if line.startswith("COPY"):
                dests.add(dest)
    return dests


def self_provided_modules() -> set[str]:
    """Dotted module names this repo itself installs into the upstream tree."""
    modules = set()
    for dest in copy_destinations():
        if dest.endswith(".py"):
            modules.add(dest[: -len(".py")].replace("/", "."))
    return modules


def self_provided_symbols() -> set[str]:
    """Classes and functions our own patches add to the upstream tree.

    These must NOT be required to appear in SYMBOLS: the preflight checks
    whether *upstream* still provides something, and asking upstream for a
    class our patch introduces would fail on every tag forever.
    """
    symbols = set()
    for patch in patch_files():
        for line in patch_added_lines(patch):
            match = re.match(r"\s*(?:async\s+)?(?:class|def)\s+(\w+)", line)
            if match:
                symbols.add(match.group(1))
    return symbols


IMPORT_RE = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+(.+)$")


def upstream_imports(lines: list[str]) -> set[tuple[str, str]]:
    """(module, symbol) pairs imported from Hermes' own source."""
    found = set()
    for line in lines:
        match = IMPORT_RE.match(line)
        if not match:
            continue
        module, imported = match.group(1), match.group(2)
        if module.split(".")[0] not in UPSTREAM_ROOTS:
            continue
        imported = imported.split("#")[0]
        for symbol in imported.replace("(", "").replace(")", "").split(","):
            symbol = symbol.strip().split(" as ")[0].strip()
            if symbol and symbol != "*":
                found.add((module, symbol))
    return found


def module_to_path(module: str) -> str:
    return module.replace(".", "/") + ".py"


def declared_imports() -> set[tuple[str, str, str]]:
    """(module, symbol, where) for every upstream import this repo makes."""
    found = set()
    for pattern in TRACKED_SOURCE_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            rel = path.relative_to(REPO_ROOT)
            for module, symbol in upstream_imports(path.read_text().splitlines()):
                found.add((module, symbol, str(rel)))
    for patch in patch_files():
        if patch.name not in applied_patches():
            continue  # only patches that reach the image constrain the build
        for module, symbol in upstream_imports(patch_added_lines(patch)):
            found.add((module, symbol, f"patches/{patch.name}"))
    return found


# --- the tests ----------------------------------------------------------


class TestManifestParses(unittest.TestCase):
    def test_deps_and_symbols_are_readable(self):
        manifest = Manifest()
        self.assertGreater(len(manifest.deps), 5)
        self.assertGreater(len(manifest.symbol_patterns), 5)
        for severity in manifest.deps.values():
            self.assertIn(severity, {"blocker", "review"})

    def test_dockerfile_apply_step_is_discoverable(self):
        # If this fails, every applied-patch assertion below silently weakens
        # to a no-op, so it is asserted directly.
        self.assertTrue(applied_patches(), "no `git apply` step found in Dockerfile")

    def test_every_symbols_path_is_also_a_deps_path(self):
        """Internal consistency, and a real trap in the script's control flow.

        The script only fetches files listed in DEPS. Its symbol loop skips any
        file it doesn't find on disk (`if [[ ! -f ... ]]; then continue`), so a
        SYMBOLS entry for a path missing from DEPS is not a loud error — it is a
        check that silently never runs, while still being counted in the
        reassuring "all N tracked symbols still present" line.
        """
        manifest = Manifest()
        for path, pattern in manifest.symbol_patterns:
            self.assertIn(
                path,
                manifest.deps,
                f"SYMBOLS tracks '{pattern}' in {path}, but {path} is not in DEPS, "
                f"so it is never fetched and the check silently no-ops",
            )


class TestPatchTargetsAreRegistered(unittest.TestCase):
    """Rule 1/2: a file we patch is a file whose shape we depend on."""

    def test_applied_patch_targets_are_blockers(self):
        manifest = Manifest()
        for patch in patch_files():
            if patch.name not in applied_patches():
                continue
            for target in patch_targets(patch):
                self.assertIn(
                    target,
                    manifest.deps,
                    f"{patch.name} patches {target}, which is missing from "
                    f"upgrade-preflight.sh's DEPS — a bump would stop checking it",
                )
                self.assertEqual(
                    manifest.deps[target],
                    "blocker",
                    f"{target} is a patch target, so any upstream diff must be a "
                    f"blocker, not '{manifest.deps[target]}'",
                )

    def test_unapplied_patch_targets_are_at_least_tracked(self):
        # line-dm-pairing.tests.patch never reaches the image (upstream ships
        # no tests/), but it is maintained and re-verified against a clone
        # during patch regeneration, so its target still needs drift reporting.
        manifest = Manifest()
        for patch in patch_files():
            if patch.name in applied_patches():
                continue
            for target in patch_targets(patch):
                self.assertIn(
                    target,
                    manifest.deps,
                    f"{patch.name} patches {target}, which is missing from DEPS",
                )


class TestCopyTargetsAreRegistered(unittest.TestCase):
    """Rule 3: we COPY into upstream directories that upstream can move."""

    def test_each_copy_destination_directory_is_covered(self):
        manifest = Manifest()
        for dest in copy_destinations():
            directory = str(Path(dest).parent)
            self.assertTrue(
                manifest.covers_directory(directory),
                f"Dockerfile COPYs into /opt/hermes/{dest}, but no DEPS entry "
                f"lives in '{directory}' — if upstream moves that package, the "
                f"preflight would not notice",
            )


class TestUpstreamImportsAreRegistered(unittest.TestCase):
    """Rule 4: anything we import by name is something upstream can rename."""

    def test_imported_modules_are_in_deps(self):
        manifest = Manifest()
        provided = self_provided_modules()
        for module, symbol, where in sorted(declared_imports()):
            if module in provided or f"{module}.{symbol}" in provided:
                continue
            self.assertIn(
                module_to_path(module),
                manifest.deps,
                f"{where} imports from '{module}', which is missing from DEPS",
            )

    def test_imported_symbols_are_in_symbols(self):
        manifest = Manifest()
        provided_modules = self_provided_modules()
        provided_symbols = self_provided_symbols()
        for module, symbol, where in sorted(declared_imports()):
            if module in provided_modules or f"{module}.{symbol}" in provided_modules:
                continue
            if symbol in provided_symbols:
                continue  # our own patch introduces it; upstream never had it
            self.assertTrue(
                manifest.covers_symbol(symbol),
                f"{where} imports '{symbol}' from '{module}', but no SYMBOLS "
                f"entry tracks it — upstream could rename it and the preflight "
                f"would report an all-clear",
            )


if __name__ == "__main__":
    unittest.main()
