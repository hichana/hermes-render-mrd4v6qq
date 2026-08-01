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

# 8b. The inverse of the above: the boot path must add skills.external_dirs and
#     NOTHING else. Specifically, no Render MCP server. A client-facing agent
#     with an `mcp_servers.render` entry can drive the Render account that hosts
#     it (plans/done/strip-render-tooling-plan.md) — the exact inversion of repo
#     CLAUDE.md's "only admins provision or manage Render resources."
#     The repo-side tooling was stripped, but a stale entry survived on the live
#     instance's volume for weeks afterwards (found 2026-07-30 during the
#     v2026.7.20 bump; `RENDER_MCP_API_KEY` was unset, so it was inert rather
#     than exploitable). /opt/data persists across deploys and this image is the
#     template for every future client, so assert the fresh-boot case is clean.
if docker exec "${CONTAINER}" grep -qE "mcp\.render\.com|RENDER_MCP_API_KEY" /opt/data/config.yaml; then
  fail "a Render MCP entry is present in a freshly booted /opt/data/config.yaml — \
something in the boot path is registering Render account access on a client-facing agent. \
Find and remove it; see plans/done/strip-render-tooling-plan.md."
fi
echo "  ok: no Render MCP entry in config.yaml"

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

# 10. Multi-channel LINE routing (line-multi-channel.patch,
#     plans/line-multi-channel-plan.md). Same reasoning as checks 6/7: LINE
#     isn't configured in this boot, so an import-time break in the patch
#     would otherwise stay invisible until it broke a live client's second
#     channel. Goes one step further than 6/7 by actually constructing a
#     multi-channel LineAdapter and driving _handle_webhook with real HMAC
#     signatures — against the actual image this Dockerfile produces, not
#     just the unit/integration test suite's own throwaway containers — to
#     prove the single property that matters most here: a payload signed
#     with one channel's secret must be rejected on a different channel's
#     route.
docker exec "${CONTAINER}" test -f /opt/hermes/plugins/platforms/line/line_multiplex.py \
  || fail "line_multiplex.py is missing from the image — multi-channel routing would fail at import"
docker exec "${CONTAINER}" grep -q "_channel_for_send" \
  /opt/hermes/plugins/platforms/line/adapter.py \
  || fail "adapter.py has no _channel_for_send call-outs — line-multi-channel.patch did not apply"
docker exec "${CONTAINER}" /opt/hermes/.venv/bin/python3 - <<'PYEOF' \
  || fail "multi-channel LINE routing is broken in the built image — see output above"
import asyncio, base64, hashlib, hmac

from aiohttp import streams
from aiohttp.base_protocol import BaseProtocol
from aiohttp.test_utils import make_mocked_request

from gateway.config import PlatformConfig
from plugins.platforms.line.adapter import LineAdapter

def sign(body, secret):
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

async def mocked_request(path, body, signature):
    loop = asyncio.get_running_loop()
    payload = streams.StreamReader(BaseProtocol(loop=loop), 2**16, loop=loop)
    payload.feed_data(body)
    payload.feed_eof()
    return make_mocked_request(
        "POST", path, headers={"X-Line-Signature": signature}, payload=payload
    )

async def main():
    import os
    os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = "default-token"
    os.environ["LINE_CHANNEL_SECRET"] = "default-secret"
    adapter = LineAdapter(PlatformConfig(enabled=True, extra={
        "channels": [{
            "profile": "smoke-second",
            "channel_secret": "second-secret",
            "channel_access_token": "second-token",
        }],
    }))
    adapter._dispatch_event = lambda event: asyncio.sleep(0)

    assert len(adapter._channels) == 2, "expected exactly 2 channels"
    assert adapter._channels.by_webhook_path("/line/webhook") is adapter._channels.default()
    assert adapter._channels.by_webhook_path("/line/p/smoke-second/webhook") is not None

    body = b'{"events": []}'

    resp = await adapter._handle_webhook(
        await mocked_request("/line/webhook", body, sign(body, "default-secret"))
    )
    assert resp.status == 200, f"default channel's own signature rejected: {resp.status}"

    resp = await adapter._handle_webhook(
        await mocked_request("/line/p/smoke-second/webhook", body, sign(body, "second-secret"))
    )
    assert resp.status == 200, f"second channel's own signature rejected: {resp.status}"

    resp = await adapter._handle_webhook(
        await mocked_request("/line/webhook", body, sign(body, "second-secret"))
    )
    assert resp.status == 401, f"CROSS-CHANNEL SIGNATURE ACCEPTED on default route: {resp.status}"

    resp = await adapter._handle_webhook(
        await mocked_request("/line/p/smoke-second/webhook", body, sign(body, "default-secret"))
    )
    assert resp.status == 401, f"CROSS-CHANNEL SIGNATURE ACCEPTED on second-channel route: {resp.status}"

    print("multi-channel routing + cross-channel signature isolation OK")

asyncio.run(main())
PYEOF
echo "  ok: multi-channel routing + cross-channel signature isolation verified live in the built image"

echo "PASS: image boots, gateway running, Caddy routing, auth armed, LINE patches live (incl. multi-channel), boot config patched"
