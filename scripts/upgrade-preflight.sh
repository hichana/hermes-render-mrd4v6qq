#!/usr/bin/env bash
# Upstream drift check for a Hermes version bump. See UPGRADING.md, Phase 1.
#
# Answers one question before you pull a multi-GB base image and build:
# did upstream move any of the specific files this repo reaches into between
# the tag we're pinned to and the tag we want?
#
# IMPORTANT — this is a CLOSED-WORLD check. It only knows about dependencies
# listed in the manifest below. It cannot tell you about a new upstream
# mechanism you haven't started depending on yet, nor about a dependency this
# repo added without registering it here. Keeping the manifest honest is part
# of the upgrade, not a chore separate from it. See UPGRADING.md Phase 5.
#
# Read-only. No Docker, no clone, no writes outside a temp dir — it fetches
# raw files from the two tags and diffs them. That is deliberately cheaper
# than patches/README.md's clone + `docker pull` + `docker cp` recipe, which
# is still the right procedure once this script confirms drift.
#
# Usage (from the repo root):
#   ./scripts/upgrade-preflight.sh <candidate-tag> [current-tag]
#
# `current-tag` defaults to the ARG HERMES_IMAGE pin in ./Dockerfile.
# Exits non-zero if there is anything that must be dealt with before building.

set -euo pipefail

RAW_BASE="${PREFLIGHT_RAW_BASE:-https://raw.githubusercontent.com/NousResearch/hermes-agent/refs/tags}"
DOCKERFILE="${PREFLIGHT_DOCKERFILE:-Dockerfile}"

# --- The manifest -------------------------------------------------------
#
# THIS is the artifact worth maintaining; the shell below it is plumbing.
# Every upstream path this repo depends on, why, and how loudly to complain.
#
#   blocker — we patch this file, COPY into it, or assert its exact shape.
#             Any diff means regenerate/rework before building.
#   review  — we only depend on named symbols inside it. It changes most
#             releases; the SYMBOLS table below carries the real assertions,
#             so a diff here is "go read it", not "stop".
#
# Format: <upstream-path>|<severity>|<what depends on it>
DEPS=(
  "plugins/platforms/line/adapter.py|blocker|patch target: line-dm-pairing.patch + line-group-mention.patch"
  "plugins/platforms/line/plugin.yaml|blocker|patch target: both patches add optional_env entries"
  "docker/main-wrapper.sh|blocker|the ENTRYPOINT chain that runs our CMD and drops to hermes"
  "docker/s6-rc.d/user/contents.d/main-hermes|blocker|proves the s6 user bundle our caddy service registers into still exists"
  "docker/s6-rc.d/user/contents.d/dashboard|blocker|same bundle; also the dashboard Caddy proxies to"
  "docker/cont-init.d/02-reconcile-profiles|blocker|our 03-render-tools hook is numbered to land after this"
  "docker/cont-init.d/015-supervise-perms|blocker|same ordering assumption"
  "Dockerfile|review|upstream image shape: ENTRYPOINT, s6, ui-tui build, HERMES_HOME"
  "hermes_constants.py|review|get_hermes_dir(), imported by render_mention.py and LineInviteStore"
  "gateway/pairing.py|review|PairingStore + generate_code(), imported by line-dm-pairing.patch"
  "gateway/platforms/base.py|review|enforces_own_access_policy, overridden by line-dm-pairing.patch"
  "gateway/run.py|review|the USR1 restart path admin-tools/env-sync verifies against"
  "hermes_cli/container_boot.py|review|profile reconciler; see HISTORICAL-GOTCHAS.md"
  "cli-config.yaml.example|review|documents the config defaults live instances inherit"
  "tests/gateway/test_line_plugin.py|review|target of line-dm-pairing.tests.patch — not in the image, but re-verified against a clone during patch regeneration"
)

# Symbols our patches, modules and admin tooling import or signal by name. A
# lost symbol is a blocker even when the file's diff looks harmless.
#
# Format: <upstream-path>|<grep -E pattern>|<label printed on loss>
SYMBOLS=(
  "hermes_constants.py|^def get_hermes_dir\(|get_hermes_dir()"
  "gateway/pairing.py|^class PairingStore\b|class PairingStore"
  "gateway/pairing.py|def generate_code\(|PairingStore.generate_code()"
  "gateway/platforms/base.py|def enforces_own_access_policy\b|enforces_own_access_policy"
  "gateway/run.py|def request_restart\(|request_restart() (env-sync restart path)"
  "gateway/run.py|SIGUSR1|SIGUSR1 handler (env-sync restart signal)"
  "gateway/run.py|gateway\.pid|gateway.pid file (env-sync restart verification)"
  "gateway/run.py|restart_drain_timeout|agent.restart_drain_timeout (env-sync poll budget)"
  "hermes_cli/container_boot.py|SOUL\.md|SOUL.md profile reconciliation"
  "plugins/platforms/line/adapter.py|async def _dispatch_event\(|_dispatch_event() (pairing patch anchor)"
  "plugins/platforms/line/adapter.py|async def _handle_message_event\(|_handle_message_event() (mention gate anchor)"
  "plugins/platforms/line/adapter.py|async def _handle_postback_event\(|_handle_postback_event() (set_line_mode anchor)"
  "plugins/platforms/line/adapter.py|^class _LineClient\b|_LineClient (get_profile, invite patch)"
  "plugins/platforms/line/adapter.py|def _truthy_env\(|_truthy_env() (patch helper)"
  "plugins/platforms/line/adapter.py|def _allowed_for_source\(|_allowed_for_source() (patch helper)"
)

# Shape assertions against the candidate's own Dockerfile. These are exactly
# the things the v2026.5.7 -> v2026.7.7.2 bump got wrong, silently, for 8 days.
#
# Format: <grep -E pattern>|<label>
STRUCTURE=(
  "ENTRYPOINT \[ ?\"/init\", ?\"/opt/hermes/docker/main-wrapper.sh\" ?\]|ENTRYPOINT is still /init + main-wrapper.sh (we must not override it)"
  "s6-overlay|s6-overlay is still the supervisor (our caddy service and cont-init hook depend on it)"
  "cont-init.d/02-reconcile-profiles|cont-init.d hooks still numbered 01/015/02, leaving 03 for us"
  "ui-tui|ui-tui is still built into the image (Dockerfile chown target)"
  "HERMES_HOME=/opt/data|HERMES_HOME is still /opt/data (the Render disk mount)"
  "useradd .*hermes|the hermes user still exists (s6-setuidgid target)"
)

# --- Plumbing -----------------------------------------------------------

CANDIDATE="${1:-}"
if [[ -z "${CANDIDATE}" ]]; then
  echo "usage: $0 <candidate-tag> [current-tag]" >&2
  exit 2
fi

pinned_tag() {
  [[ -f "${DOCKERFILE}" ]] || return 1
  sed -n 's/^ARG HERMES_IMAGE=.*:\([^ ]*\)$/\1/p' "${DOCKERFILE}" | head -1
}

CURRENT="${2:-}"
if [[ -z "${CURRENT}" ]]; then
  CURRENT="$(pinned_tag || true)"
  if [[ -z "${CURRENT}" ]]; then
    echo "FAIL: could not read the current pin from ./${DOCKERFILE} — pass it explicitly" >&2
    exit 2
  fi
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

blockers=0
reviews=0

fetch() { # tag path outfile
  curl -sfL --retry 2 "${RAW_BASE}/$1/$2" -o "$3" 2>/dev/null
}

local_path() { # tag path
  echo "${WORK}/$1/$(echo "$2" | tr / _)"
}

echo "==> Preflight: ${CURRENT} -> ${CANDIDATE}"
echo "    source: ${RAW_BASE}"

# Both tags must resolve before anything else, or a fetch failure downstream
# reads as "upstream deleted the file" instead of "you typo'd the tag".
for tag in "${CURRENT}" "${CANDIDATE}"; do
  mkdir -p "${WORK}/${tag}"
  if ! fetch "${tag}" Dockerfile "$(local_path "${tag}" Dockerfile)"; then
    echo "FAIL: tag '${tag}' not found at ${RAW_BASE} (no Dockerfile there)" >&2
    exit 1
  fi
done
echo "  ok: both tags resolve"

echo
echo "==> File drift"
for entry in "${DEPS[@]}"; do
  IFS='|' read -r path severity note <<<"${entry}"
  old="$(local_path "${CURRENT}" "${path}")"
  new="$(local_path "${CANDIDATE}" "${path}")"
  [[ -f "${old}" ]] || fetch "${CURRENT}" "${path}" "${old}" || true
  [[ -f "${new}" ]] || fetch "${CANDIDATE}" "${path}" "${new}" || true

  if [[ ! -f "${new}" ]]; then
    echo "  MISSING(blocker): ${path} — not present at ${CANDIDATE} (${note})"
    blockers=$((blockers + 1))
    continue
  fi
  if [[ ! -f "${old}" ]]; then
    echo "  NEW: ${path} — absent at ${CURRENT}, present at ${CANDIDATE} (nothing to compare)"
    continue
  fi
  if cmp -s "${old}" "${new}"; then
    echo "  ok: ${path} unchanged"
    continue
  fi
  churn="$(diff -u "${old}" "${new}" | grep -cE '^[+-][^+-]' || true)"
  if [[ "${severity}" == "blocker" ]]; then
    echo "  DRIFT(blocker): ${path} — ${churn} changed lines. ${note}"
    blockers=$((blockers + 1))
  else
    echo "  DRIFT(review): ${path} — ${churn} changed lines. ${note}"
    reviews=$((reviews + 1))
  fi
done

echo
echo "==> Symbols we import by name"
lost=0
for entry in "${SYMBOLS[@]}"; do
  IFS='|' read -r path pattern label <<<"${entry}"
  new="$(local_path "${CANDIDATE}" "${path}")"
  if [[ ! -f "${new}" ]]; then
    continue # already reported as MISSING above
  fi
  if ! grep -qE "${pattern}" "${new}"; then
    echo "  SYMBOL LOST(blocker): ${label} — gone from ${path} at ${CANDIDATE}"
    blockers=$((blockers + 1))
    lost=$((lost + 1))
  fi
done
[[ "${lost}" -eq 0 ]] && echo "  ok: all ${#SYMBOLS[@]} tracked symbols still present"

echo
echo "==> Image shape"
broken=0
candidate_dockerfile="$(local_path "${CANDIDATE}" Dockerfile)"
for entry in "${STRUCTURE[@]}"; do
  IFS='|' read -r pattern label <<<"${entry}"
  if ! grep -qE "${pattern}" "${candidate_dockerfile}"; then
    echo "  STRUCTURE(blocker): ${label} — no longer true at ${CANDIDATE}"
    blockers=$((blockers + 1))
    broken=$((broken + 1))
  fi
done
[[ "${broken}" -eq 0 ]] && echo "  ok: all ${#STRUCTURE[@]} image-shape assumptions still hold"

echo
echo "==> Config defaults live instances inherit"
# Filter to actual key: value lines so a changed *default* surfaces instead of
# drowning in the comment churn that dominates this file's diff. Comments are
# excluded on purpose — a rewritten explanation is not a behavior change.
old_cfg="$(local_path "${CURRENT}" cli-config.yaml.example)"
new_cfg="$(local_path "${CANDIDATE}" cli-config.yaml.example)"
if [[ -f "${old_cfg}" && -f "${new_cfg}" ]]; then
  defaults="$(diff -u "${old_cfg}" "${new_cfg}" \
    | grep -E '^[+-][[:space:]]*[a-zA-Z_]+:' \
    | grep -vE '^[+-][[:space:]]*#' || true)"
  if [[ -n "${defaults}" ]]; then
    echo "${defaults}" | sed 's/^/  CONFIG DEFAULT: /'
    echo "  ^ these are DEFAULTS, not our settings. They only bite a live"
    echo "    instance whose /opt/data/config.yaml inherits them rather than"
    echo "    setting them explicitly. See UPGRADING.md Phase 4."
  else
    echo "  ok: no default values changed"
  fi
else
  echo "  skipped: cli-config.yaml.example unavailable at one or both tags"
fi

echo
echo "PREFLIGHT: ${blockers} blockers, ${reviews} to review"
if [[ "${blockers}" -gt 0 ]]; then
  echo "Do not build yet — see UPGRADING.md Phase 1 for what each blocker means." >&2
  exit 1
fi
