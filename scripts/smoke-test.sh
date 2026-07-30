#!/usr/bin/env bash
# Boot smoke test for the hermes-render image.
#
# Builds the image and boots it the way Render does, then asserts the
# things that must hold for a deploy to be healthy:
#
#   1. The container stays up (the entrypoint chain actually runs the CMD).
#   2. The gateway reaches the running state.
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
# Mirror render.yaml: Caddy owns 10000, dashboard on 10001 bound 0.0.0.0
# (non-loopback, so the auth gate stays armed).
#
# --security-opt no-new-privileges / --cap-drop NET_BIND_SERVICE mimic
# Render's runtime, which is stricter than stock `docker run`. Without
# these, this test passes on an image that fails to boot on Render: the
# Caddy binary shipped with cap_net_bind_service and exec'ing it as a
# non-root user died with "unable to exec caddy: Operation not permitted"
# in production while a stock local run was happy. Nothing here binds a
# port below 1024, so dropping the capability costs nothing.
docker run -d --name "${CONTAINER}" --platform "${PLATFORM}" \
  --security-opt no-new-privileges \
  --cap-drop NET_BIND_SERVICE \
  -e HERMES_DASHBOARD=1 \
  -e HERMES_DASHBOARD_HOST=0.0.0.0 \
  -e HERMES_DASHBOARD_PORT=10001 \
  -e HERMES_DASHBOARD_BASIC_AUTH_USERNAME=smoke \
  -e HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=smoke-test-password \
  "${IMAGE}" >/dev/null

echo "==> Waiting for gateway (up to ${BOOT_TIMEOUT}s)"
deadline=$((SECONDS + BOOT_TIMEOUT))
while (( SECONDS < deadline )); do
  if ! docker ps --filter "name=${CONTAINER}" --format '{{.Names}}' | grep -q "${CONTAINER}"; then
    docker logs "${CONTAINER}" 2>&1 | tail -25 >&2
    fail "container exited (entrypoint chain did not run the CMD)"
  fi
  # Probe through Caddy on 10000, which also proves the proxy is routing.
  state="$(docker exec "${CONTAINER}" curl -s --max-time 5 http://127.0.0.1:10000/api/status 2>/dev/null \
    | sed -n 's/.*"gateway_state": *"\([^"]*\)".*/\1/p' || true)"
  [[ "${state}" == "running" ]] && break
  sleep 5
done

# 1. Container still up.
docker ps --filter "name=${CONTAINER}" --format '{{.Names}}' | grep -q "${CONTAINER}" \
  || fail "container is not running"
echo "  ok: container is up"

# 2. Gateway reached running. Probed through Caddy, so this also proves the
#    catch-all route reaches the dashboard.
[[ "${state}" == "running" ]] || fail "gateway_state is '${state:-unknown}', expected 'running'"
echo "  ok: gateway_state=running (via Caddy :10000 -> dashboard)"

# 3. Caddy is the thing on 10000, and the dashboard is NOT publicly bound
#    there. Guards against a future edit that puts the dashboard back on
#    10000 and quietly drops the /line/* route.
docker exec "${CONTAINER}" sh -c 'command -v caddy >/dev/null' \
  || fail "caddy is not installed in the image"
docker exec "${CONTAINER}" ps -eo args 2>/dev/null | grep -q "caddy run" \
  || fail "caddy is not running (check the s6 service registered in the user bundle)"
echo "  ok: caddy is running and owns :10000"

# 4. The auth gate is armed. The dashboard sits behind Caddy, which forwards
#    the public internet to it, so an unauthenticated dashboard would expose
#    provider keys and a PTY. /api/status is deliberately open (health
#    check); anything else must not be.
code="$(docker exec "${CONTAINER}" curl -s -o /dev/null -w '%{http_code}' \
  --max-time 5 http://127.0.0.1:10000/api/keys 2>/dev/null || true)"
[[ "${code}" == "401" || "${code}" == "403" ]] \
  || fail "dashboard auth gate is NOT armed: /api/keys returned ${code}, expected 401/403. \
Check HERMES_DASHBOARD_HOST is non-loopback (the gate only engages on non-loopback binds)."
echo "  ok: dashboard auth gate armed (/api/keys -> ${code})"

# 5. The /line/* route reaches the LINE adapter's port, not the dashboard.
#    LINE is not configured here, so nothing listens on 8646 and Caddy
#    returns a 502. That is the point: a 502 proves Caddy routed to the LINE
#    backend, whereas a 302 would mean the request fell through to the
#    dashboard's login redirect — the exact bug this route exists to fix.
code="$(docker exec "${CONTAINER}" curl -s -o /dev/null -w '%{http_code}' \
  --max-time 5 http://127.0.0.1:10000/line/webhook/health 2>/dev/null || true)"
case "${code}" in
  502|503) echo "  ok: /line/* routes to the LINE backend (${code}; LINE not configured here)" ;;
  200)     echo "  ok: /line/* routes to a live LINE webhook server (200)" ;;
  30*)     fail "/line/webhook/health returned ${code} — it is hitting the DASHBOARD, not the LINE backend" ;;
  *)       fail "/line/webhook/health returned unexpected ${code}" ;;
esac

# 6. The LINE group mention gate is wired. Two separate failure modes, both
#    invisible in `docker logs`:
#      (a) render_mention.py missing or landed at the wrong path — the COPY
#          targets a directory inside the upstream package, which upstream is
#          free to move;
#      (b) present but not importable as part of the plugins.platforms.line
#          package, which is exactly what the adapter's `from
#          plugins.platforms.line import render_mention` needs. Asserting the
#          real import (not just the file's existence) is the whole point:
#          Hermes is an editable install, so importability is a property of
#          the install layout, not of the file being on disk.
docker exec "${CONTAINER}" test -f /opt/hermes/plugins/platforms/line/render_mention.py \
  || fail "render_mention.py is missing from the image — the mention gate would fail at import"
docker exec "${CONTAINER}" /opt/hermes/.venv/bin/python3 -c \
  'import plugins.platforms.line.render_mention as m; assert m.MODE_MENTION == "mention"' \
  >/dev/null 2>&1 \
  || fail "render_mention.py is present but not importable as plugins.platforms.line.render_mention"
echo "  ok: mention-gate module present and importable"

# 7. The call-outs landed in the adapter AND the patched adapter still
#    imports. The import half matters more than the grep: LINE is not
#    configured in this smoke run, so the adapter is never instantiated at
#    boot — an ImportError introduced by the patch would stay invisible here
#    and first surface as a dead LINE channel on a live client instance.
docker exec "${CONTAINER}" grep -q "_mention_gate" \
  /opt/hermes/plugins/platforms/line/adapter.py \
  || fail "adapter.py has no _mention_gate call-outs — line-group-mention.patch did not apply"
docker exec "${CONTAINER}" /opt/hermes/.venv/bin/python3 -c \
  'import plugins.platforms.line.adapter as a; assert a.render_mention.MODE_MENTION == "mention"' \
  >/dev/null 2>&1 \
  || fail "the patched LINE adapter does not import — the mention-gate call-outs are broken"
echo "  ok: mention-gate call-outs present, patched adapter imports"

# 8. The boot-time config patcher actually ran and landed. Asserting the live
#    file rather than a log line (ARCHITECTURE.md's Pattern 4) covers three
#    independent upgrade-fragile assumptions at once: patch-config.py's
#    `#!/opt/hermes/.venv/bin/python` shebang still resolves, PyYAML still
#    ships in Hermes' venv, and /etc/cont-init.d/03-render-tools still sorts
#    after upstream's 01/015/02 hooks. All three are invisible in `docker
#    logs` — the hook swallows its own failures on purpose so a bad patcher
#    can never keep the agent from booting.
docker exec "${CONTAINER}" grep -q "/opt/render-tools/skills-local" /opt/data/config.yaml \
  || fail "skills.external_dirs was not patched into /opt/data/config.yaml — \
the line-invite skill will not be discoverable. Check the 03-render-tools cont-init hook \
and patch-config.py's shebang/PyYAML assumptions."
echo "  ok: skills.external_dirs patched into config.yaml"

# 9. The line-invite skill's script can actually import what it needs.
#    LineInviteStore exists ONLY because of line-dm-pairing.patch, and
#    `qrcode` only because upstream happens to ship it — an upgrade can drop
#    that dependency, and without this check we'd first hear about it from a
#    client's manager whose QR invite command failed.
docker exec "${CONTAINER}" /opt/hermes/.venv/bin/python3 -c \
  "import sys; sys.path.insert(0, '/opt/hermes'); \
from plugins.platforms.line.adapter import LineInviteStore; import qrcode" \
  >/dev/null 2>&1 \
  || fail "the line-invite skill cannot import its dependencies — either \
LineInviteStore is missing (line-dm-pairing.patch did not apply) or qrcode is no longer \
in Hermes' venv. See skills/line-invite/scripts/generate_invite.py."
echo "  ok: line-invite skill dependencies importable"

# Note: there used to be a 6th check here confirming a boot-time seeder
# copied a Render env var into /opt/data/.env. That mechanism (insert-only,
# went silently stale on any later edit) was replaced by admin-tools/env-sync,
# which upserts a live, provisioned Render instance over real SSH — out of
# scope for this local Docker boot smoke test.

echo "PASS: image boots, gateway running, Caddy routing, auth armed, LINE patches live, boot config patched"
