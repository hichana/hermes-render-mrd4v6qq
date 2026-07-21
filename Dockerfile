# syntax=docker/dockerfile:1.7
#
# Hermes Agent on Render.
#
# Extends the upstream NousResearch/hermes-agent image with:
#   - A permissions fix so the dashboard's Chat tab can rebuild at runtime
#   - A Caddy-based path multiplexer for Render's single exposed port
#     (dashboard + LINE webhook)
#
# This image deliberately carries no Render account access (no MCP server,
# no API key, no `render` CLI). Provisioning/managing Render resources is
# an admin-only action taken outside the deployed agent, via the Render
# dashboard or CLI directly.
#
# Pin the upstream tag here. Bump and redeploy to upgrade Hermes.
ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent:v2026.7.7.2
ARG CADDY_IMAGE=docker.io/library/caddy:2.10-alpine
FROM ${CADDY_IMAGE} AS caddy
FROM ${HERMES_IMAGE}

# The dashboard runs as `hermes`, but ui-tui/ and node_modules/ still ship
# root-owned (upstream #20500). Without this the Chat tab's runtime esbuild
# rebuild fails with EACCES.
#
# The old `touch ink-bundle.js` / `touch entry.js` workarounds are gone as of
# v2026.7.7.2: _hermes_ink_bundle_stale() and _tui_build_needed() no longer
# exist, and the image now ships a prebuilt ui-tui/dist/entry.js, which
# hermes_cli/main.py treats as "the single runtime artefact" (prebuilt bundle
# mode). Nothing reads ink-bundle.js anymore.
USER root
RUN chown -R hermes:hermes /opt/hermes/ui-tui /opt/hermes/node_modules

# Path-based multiplexer for Render's single exposed port. Render routes
# only port 10000, but the LINE adapter's webhook server must be publicly
# reachable (LINE POSTs to it) AND the dashboard needs to stay usable.
# Caddy owns 10000 and fans out by path. Render publishes no other port, so
# the backends are unreachable from outside regardless of their bind host.
#
# The binary comes from the official Caddy image (static Go, no runtime
# deps) rather than a curl|tar of a GitHub release, so the version is
# pinned by CADDY_IMAGE and updates through the normal image flow.
#
# `cat` rather than a plain COPY to strip the binary's file capabilities.
# The official image ships caddy with cap_net_bind_service=ep, and the
# security.capability xattr survives COPY. Under a restrictive runtime
# (no-new-privileges, or a bounding set without that cap — Render is
# stricter than stock Docker here) exec'ing a file-capability binary as a
# non-root user fails with EPERM:
#
#   s6-applyuidgid: fatal: unable to exec caddy: Operation not permitted
#
# Caddy binds :10000, well above 1024, so it never needed the capability.
# Copying through `cat` creates a fresh inode with no xattrs. NOTE this
# reproduces only under such a runtime, not on stock `docker run`.
COPY --from=caddy /usr/bin/caddy /tmp/caddy.orig
RUN cat /tmp/caddy.orig > /usr/bin/caddy \
 && chmod 0755 /usr/bin/caddy \
 && rm /tmp/caddy.orig
COPY --chown=root:root caddy/Caddyfile /etc/caddy/Caddyfile
COPY --chown=root:root caddy/s6-rc.d/caddy /etc/s6-overlay/s6-rc.d/caddy
# Register with the `user` bundle, alongside upstream's dashboard and
# main-hermes services. An empty file named after the service is how s6-rc
# declares bundle membership.
RUN chmod 0755 /etc/s6-overlay/s6-rc.d/caddy/run /etc/s6-overlay/s6-rc.d/caddy/finish \
 && touch /etc/s6-overlay/s6-rc.d/user/contents.d/caddy \
 && install -d -o hermes -g hermes -m 0755 \
      /var/lib/caddy /var/lib/caddy/.config /var/lib/caddy/.local/share \
 && caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

# Pre-create HERMES_HOME so chown works cleanly on first boot. The
# mounted disk replaces this empty dir at runtime; baking it just keeps
# the image self-contained for any non-disk use.
RUN install -d -o hermes -g hermes -m 0755 /opt/data

# Deliberately NO ENTRYPOINT override. The image's own ENTRYPOINT is
# ["/init", "/opt/hermes/docker/main-wrapper.sh"] (s6-overlay): /init runs
# the cont-init hooks as root, starts the supervised services (dashboard,
# per-profile gateways, caddy), then main-wrapper.sh runs this CMD and
# drops to the hermes user via s6-setuidgid.
#
# Overriding ENTRYPOINT here is what broke the v2026.5.7 → v2026.7.7.2
# upgrade: /usr/bin/tini is now a symlink to /init, so the old
# `tini -g -- bootstrap.sh` line resolved to `/init -g -- ...` and s6 tried
# to run `-g` as the main program (exit 127). Leave the image's own
# ENTRYPOINT alone; put boot work in /etc/cont-init.d/ instead.
CMD ["gateway", "run"]
