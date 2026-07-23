#!/command/with-contenv sh
# cont-init hook for the render-tools skill overlay, installed as
# /etc/cont-init.d/03-render-tools.
#
# Shebang is with-contenv, not plain sh: s6-overlay v3 cont-init scripts
# do NOT get Render/docker `-e` env vars for free -- they're captured into
# /run/s6/container_environment/ files but only actually exported into a
# script's process environment via with-contenv. Confirmed the hard way:
# the .env seeder below silently saw an empty LINE_BASIC_ID at real boot
# (via s6-setuidgid) despite `docker exec -u hermes` seeing it fine --
# exec goes through a different path than a cont-init-stage script does.
#
# Runs as root under s6-overlay's cont-init stage, after the upstream
# 01-hermes-setup hook has chowned the volume and seeded $HERMES_HOME,
# and before s6-rc starts the user services. On every boot it:
#   1. Ensures $HERMES_HOME exists and is owned by hermes:hermes.
#   2. Runs the config patcher as the hermes user. The patcher is
#      idempotent: it only INSERTs the skills.external_dirs entry; it
#      never overwrites user edits (repo CLAUDE.md Pattern 1).
#   3. Runs the .env seeder as the hermes user, copying a small allowlist
#      of per-instance Render env vars (e.g. LINE_BASIC_ID) into .env the
#      first time -- also insert-only, same Pattern 1 guarantee.
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
ENV_SEEDER="/opt/render-tools/seed-env-from-render.py"

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

# Seed .env from a small allowlist of Render env vars (e.g. LINE_BASIC_ID).
# Same never-fail-the-boot reasoning as the config patcher above: the agent
# still runs without this, and an operator can always paste the value into
# the dashboard's API Keys tab by hand instead.
if [ -x "${ENV_SEEDER}" ]; then
  if ! s6-setuidgid hermes "${ENV_SEEDER}" "${DATA_DIR}/.env"; then
    echo "[render-tools] warning: env seed failed; continuing with unmodified .env" >&2
  fi
else
  echo "[render-tools] warning: ${ENV_SEEDER} not found or not executable; skipping" >&2
fi
