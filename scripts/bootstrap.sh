#!/bin/sh
# cont-init hook for the render-tools skill overlay, installed as
# /etc/cont-init.d/03-render-tools.
#
# Runs as root under s6-overlay's cont-init stage, after the upstream
# 01-hermes-setup hook has chowned the volume and seeded $HERMES_HOME,
# and before s6-rc starts the user services. On every boot it:
#   1. Ensures $HERMES_HOME exists and is owned by hermes:hermes.
#   2. Runs the config patcher as the hermes user. The patcher is
#      idempotent: it only INSERTs the skills.external_dirs entry; it
#      never overwrites user edits (repo CLAUDE.md Pattern 1).
#
# This hook must NOT exec the CMD. Under s6-overlay the image's own
# ENTRYPOINT (/init + main-wrapper.sh) runs the CMD after cont-init
# finishes, and the dashboard + per-profile gateways come up as
# supervised s6 services. A hook that execs would pre-empt all of that
# (repo CLAUDE.md Pattern 2).
#
# Privilege drop uses s6-setuidgid, not gosu: gosu is not present in the
# s6-based image (upstream's own stage2-hook.sh uses s6-setuidgid too).

set -eu

DATA_DIR="${HERMES_HOME:-/opt/data}"
PATCHER="/opt/render-tools/patch-config.py"

# Make sure the data dir exists and the hermes user can write to it
# before we run the patcher. Idempotent — if this is already a mounted,
# chowned disk this is a no-op.
mkdir -p "${DATA_DIR}"
if ! chown -R hermes:hermes "${DATA_DIR}" 2>/dev/null; then
  echo "[render-tools] warning: could not chown ${DATA_DIR}; continuing" >&2
fi

# Patch config.yaml. We never fail the boot on a patch error — the agent
# can still run without the skill registered, and the user can always
# add the external dir manually from the dashboard.
if [ -x "${PATCHER}" ]; then
  if ! s6-setuidgid hermes "${PATCHER}" "${DATA_DIR}/config.yaml"; then
    echo "[render-tools] warning: config patch failed; continuing with unmodified config" >&2
  fi
else
  echo "[render-tools] warning: ${PATCHER} not found or not executable; skipping" >&2
fi
