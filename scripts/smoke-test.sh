#!/usr/bin/env bash
# Boot smoke test for the render-tools image.
#
# Builds the image and boots it the way Render does, then asserts the
# three things that must hold for a deploy to be healthy:
#
#   1. The container stays up (the entrypoint chain actually runs the CMD).
#   2. Our cont-init hook patched config.yaml (Render MCP registered).
#   3. The gateway reaches the running state.
#
# Run this after bumping HERMES_IMAGE, before deploying. The
# v2026.5.7 → v2026.7.7.2 bump broke all three at once — silently, in
# ways the dashboard still looked healthy through — because upstream
# switched to s6-overlay, repointed /usr/bin/tini at /init, and dropped
# gosu from the image.
#
# Usage: ./scripts/smoke-test.sh [image-tag]

set -euo pipefail

IMAGE="${1:-hermes-render:smoke}"
CONTAINER="hermes-smoke-$$"
PLATFORM="linux/amd64"  # Render runs amd64; the tini/init symlink is arch-independent but boot paths are not
BOOT_TIMEOUT=90

cleanup() { docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "==> Building ${IMAGE}"
docker build --platform "${PLATFORM}" -t "${IMAGE}" . >/dev/null

echo "==> Booting container"
docker run -d --name "${CONTAINER}" --platform "${PLATFORM}" \
  -e HERMES_DASHBOARD=1 \
  -e HERMES_DASHBOARD_HOST=0.0.0.0 \
  -e HERMES_DASHBOARD_PORT=10000 \
  -e HERMES_DASHBOARD_BASIC_AUTH_USERNAME=smoke \
  -e HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=smoke-test-password \
  -e RENDER_MCP_API_KEY=smoke-test-key \
  "${IMAGE}" >/dev/null

echo "==> Waiting for gateway (up to ${BOOT_TIMEOUT}s)"
deadline=$((SECONDS + BOOT_TIMEOUT))
while (( SECONDS < deadline )); do
  if ! docker ps --filter "name=${CONTAINER}" --format '{{.Names}}' | grep -q "${CONTAINER}"; then
    docker logs "${CONTAINER}" 2>&1 | tail -25 >&2
    fail "container exited (entrypoint chain did not run the CMD)"
  fi
  state="$(docker exec "${CONTAINER}" curl -s --max-time 5 http://127.0.0.1:10000/api/status 2>/dev/null \
    | sed -n 's/.*"gateway_state": *"\([^"]*\)".*/\1/p' || true)"
  [[ "${state}" == "running" ]] && break
  sleep 5
done

# 1. Container still up.
docker ps --filter "name=${CONTAINER}" --format '{{.Names}}' | grep -q "${CONTAINER}" \
  || fail "container is not running"
echo "  ok: container is up"

# 2. cont-init hook ran and patched config.yaml. Checked independently of
#    the log line, since a failed privilege drop (e.g. gosu removed
#    upstream) still lets the container look healthy.
docker exec "${CONTAINER}" grep -q "mcp.render.com" /opt/data/config.yaml \
  || { docker logs "${CONTAINER}" 2>&1 | grep -i 'render-tools' >&2 || true
       fail "config.yaml is missing the Render MCP server (cont-init hook did not patch)"; }
echo "  ok: config.yaml has the Render MCP server"

docker exec "${CONTAINER}" grep -q "/opt/render-tools/skills-upstream" /opt/data/config.yaml \
  || fail "config.yaml is missing skills.external_dirs"
echo "  ok: config.yaml has skills.external_dirs"

# 3. Gateway reached running.
[[ "${state}" == "running" ]] || fail "gateway_state is '${state:-unknown}', expected 'running'"
echo "  ok: gateway_state=running"

echo "PASS: image boots, config patched, gateway running"
