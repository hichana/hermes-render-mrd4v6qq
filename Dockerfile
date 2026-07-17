# syntax=docker/dockerfile:1.7
#
# Hermes Agent on Render, pre-baked with Render tooling.
#
# Extends the upstream NousResearch/hermes-agent image with:
#   - A bundle of Render-focused skills mounted via skills.external_dirs
#   - A boot-time patcher that registers the Render MCP server in
#     config.yaml (idempotent; never overwrites user edits)
#
# We deliberately do NOT install the `render` CLI. This image is configured
# around the Render MCP server; installing extra CLIs should be a conscious
# operator choice, not something the agent does as an automatic fallback.
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

# Pull the official Render skill bundle from github.com/render-oss/skills
# at a pinned commit. Mounted via skills.external_dirs at boot, so the
# upstream Hermes skills-sync flow never touches these files. To upgrade,
# bump RENDER_SKILLS_REF (a commit SHA, tag, or branch) and rebuild.
ARG RENDER_SKILLS_REPO=render-oss/skills
ARG RENDER_SKILLS_REF=1b8496570748203351f628b2ae738805ac2c23d5
RUN set -eu; \
    tmp="$(mktemp -d)"; \
    url="https://codeload.github.com/${RENDER_SKILLS_REPO}/tar.gz/${RENDER_SKILLS_REF}"; \
    curl -fsSL --retry 3 -o "${tmp}/skills.tar.gz" "${url}"; \
    tar -xzf "${tmp}/skills.tar.gz" -C "${tmp}"; \
    extracted="$(find "${tmp}" -maxdepth 2 -type d -name 'skills' | head -n 1)"; \
    test -n "${extracted}" || { echo "could not find skills/ in tarball" >&2; exit 1; }; \
    install -d -o hermes -g hermes -m 0755 /opt/render-tools/skills-upstream; \
    cp -a "${extracted}/." /opt/render-tools/skills-upstream/; \
    chown -R hermes:hermes /opt/render-tools/skills-upstream; \
    rm -rf "${tmp}"; \
    echo "${RENDER_SKILLS_REPO}@${RENDER_SKILLS_REF}" > /opt/render-tools/skills-upstream/.source

# Local overlay: a Hermes-specific `render-on-hermes` skill that tells
# the agent the MCP server is pre-wired (so skip "install MCP" from
# upstream skills) and that the CLI is deliberately absent (so don't
# try to invoke it). Listed FIRST in skills.external_dirs so same-named
# overlays would shadow upstream entries.
COPY --chown=hermes:hermes skills/ /opt/render-tools/skills-local/

# Boot-time config patcher, installed as an s6-overlay cont-init hook.
# Upstream ships /etc/cont-init.d/{01-hermes-setup,015-supervise-perms,
# 02-reconcile-profiles}; hooks run in lexical order, so 03- lands after
# the volume is chowned and $HERMES_HOME is seeded.
COPY --chown=root:root scripts/bootstrap.sh /etc/cont-init.d/03-render-tools
COPY --chown=root:root scripts/patch-config.py /opt/render-tools/patch-config.py
RUN chmod 0755 /etc/cont-init.d/03-render-tools /opt/render-tools/patch-config.py

# Path-based multiplexer for Render's single exposed port. Render routes
# only port 10000, but the LINE adapter's webhook server must be publicly
# reachable (LINE POSTs to it) AND the dashboard needs to stay usable.
# Caddy owns 10000 and fans out by path; both backends bind loopback only.
#
# The binary comes from the official Caddy image (static Go, no runtime
# deps) rather than a curl|tar of a GitHub release, so the version is
# pinned by CADDY_IMAGE and updates through the normal image flow.
COPY --from=caddy /usr/bin/caddy /usr/bin/caddy
COPY --chown=root:root caddy/Caddyfile /etc/caddy/Caddyfile
COPY --chown=root:root caddy/s6-rc.d/caddy /etc/s6-overlay/s6-rc.d/caddy
# Register with the `user` bundle, alongside upstream's dashboard and
# main-hermes services. An empty file named after the service is how s6-rc
# declares bundle membership.
RUN chmod 0755 /etc/s6-overlay/s6-rc.d/caddy/run \
 && touch /etc/s6-overlay/s6-rc.d/user/contents.d/caddy \
 && caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

# Pre-create the dir the patcher writes to so chown works cleanly on
# first boot. The mounted disk replaces this empty dir at runtime;
# baking it just keeps the image self-contained for any non-disk use.
RUN install -d -o hermes -g hermes -m 0755 /opt/data

# Deliberately NO ENTRYPOINT override. The image's own ENTRYPOINT is
# ["/init", "/opt/hermes/docker/main-wrapper.sh"] (s6-overlay): /init runs
# the cont-init hooks (including ours, as root), starts the supervised
# services (dashboard, per-profile gateways), then main-wrapper.sh runs
# this CMD and drops to the hermes user via s6-setuidgid.
#
# Overriding ENTRYPOINT here is what broke the v2026.5.7 → v2026.7.7.2
# upgrade: /usr/bin/tini is now a symlink to /init, so the old
# `tini -g -- bootstrap.sh` line resolved to `/init -g -- ...` and s6 tried
# to run `-g` as the main program (exit 127). Leave the image's own
# ENTRYPOINT alone; put boot work in /etc/cont-init.d/ instead.
CMD ["gateway", "run"]
