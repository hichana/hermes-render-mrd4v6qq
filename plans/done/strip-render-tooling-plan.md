# Strip Render MCP / skills tooling from the client Hermes template

## Context

This repo is the template used to spin up one Hermes Agent instance per
client business. It currently bakes in full, unscoped Render account
access: a boot-time patcher registers a Render MCP server (`mcp_servers.render`)
with **no `tools.include` filter**, authenticated via `RENDER_MCP_API_KEY`,
plus two skill bundles (`render-oss/skills` upstream, and this repo's own
`skills/render-on-hermes/SKILL.md` overlay) that teach the agent how to use
those tools including mutating ones (`restart_service`, `create_web_service`,
`trigger_deploy`, `query_render_postgres`, etc.).

Decision from discussion: **only admins should ever provision/manage Render
resources** — never a client-facing agent instance. A separate "admin"
build variant isn't needed either; admin Render operations can be done
directly via the Render dashboard/CLI, without a live, always-on Hermes
instance carrying that capability. So this isn't a gate-behind-a-flag
job — it's a straight removal. The template should stop being "Hermes
pre-baked with Render tools" and just be "Hermes on Render," with no
special Render account access at all.

Important nuance already learned: removing `skills/render-on-hermes/SKILL.md`
alone would do nothing, since skills are guidance text, not an access
control mechanism. The actual gate is the MCP server registration
(`mcp_servers.render` in `config.yaml`) and the `RENDER_MCP_API_KEY` that
authenticates it. Both the skill *and* the registration/key have to go
together, or a client could still ask the agent to reach for
`mcp_render_*` tools that are live but merely undocumented.

## Non-goals

- No admin/client Dockerfile variant, build ARG toggle, or multi-target
  build. Just delete the tooling.
- No change to the Caddy path-multiplexer, LINE webhook routing, dashboard
  auth gate, or per-profile gateway logic — all unrelated to Render MCP.
- No change to `/opt/data` persistent-disk handling beyond what's directly
  tied to the removed patcher (see the one open question below).

## Files to change

### Delete entirely
- `skills/render-on-hermes/SKILL.md` (and the now-empty `skills/` dir)
- `scripts/bootstrap.sh` — the cont-init hook that only exists to run the patcher
- `scripts/patch-config.py` — the config patcher
- `tests/test_patch_config.py` — tests the module being deleted

### `Dockerfile`
- Remove lines 32–49 (the `RENDER_SKILLS_REPO`/`RENDER_SKILLS_REF` ARGs and
  the `RUN` block that curls/extracts the `render-oss/skills` tarball into
  `/opt/render-tools/skills-upstream`).
- Remove lines 51–56 (`COPY skills/ → /opt/render-tools/skills-local/`).
- Remove lines 58–64 (`COPY scripts/bootstrap.sh → .../03-render-tools`,
  `COPY scripts/patch-config.py`, and the chmod).
- Rewrite the header comment (lines 1–12): drop "pre-baked with Render
  tooling" and the CLI-avoidance rationale (both no longer apply — there's
  no Render tooling left to avoid confusing with a CLI). Replace with a
  short description of what the image actually still adds: the dashboard
  permission fix and the Caddy port multiplexer.
- Leave untouched: `HERMES_IMAGE`/`CADDY_IMAGE` ARGs and the two-stage
  `FROM` (lines 15–18), the `ui-tui`/`node_modules` chown fix (20–30), the
  entire Caddy section (66–101), the `CMD`/entrypoint commentary (108–119).
- **Open question, resolve during execution:** lines 103–106
  (`RUN install -d -o hermes -g hermes -m 0755 /opt/data`) are commented as
  "pre-create the dir the patcher writes to." `bootstrap.sh`'s own comment
  says upstream's `01-hermes-setup` hook already chowns the volume before
  our hook runs — which suggests this line might be fully redundant once
  the patcher is gone, or might still matter for the image-only (no-disk)
  case the comment also mentions. Default: **keep it** (cheap, low blast
  radius) unless the smoke test shows it's unnecessary; don't remove on
  faith.

### `render.yaml`
- Remove the `RENDER_MCP_API_KEY` env var block (lines 111–122).
- Rewrite the header comment (lines 1–24): drop the `render-oss/skills`
  bundle bullet, the "Render MCP server pre-registered" bullet, and the
  "To upgrade Render skills: bump `RENDER_SKILLS_REF`" line.
- In the dashboard-admin-surface comment (lines 89–91), drop the clause
  "Anyone who reaches it can drive the agent with your `RENDER_MCP_API_KEY`"
  — the dashboard is still an admin surface (it holds LLM provider keys and
  a PTY), just not a Render one anymore.

### `.env.example`
- Remove the `RENDER_MCP_API_KEY=` block (lines 43–54).

### `scripts/smoke-test.sh`
- Remove `-e RENDER_MCP_API_KEY=smoke-test-key` from the `docker run` args
  (line 53).
- Remove assertion 2b, the `skills-upstream` check (lines 83–85).
- Remove assertion 2, the `mcp.render.com` check (lines 75–81), and
  renumber the remaining assertions (currently 3–6) down by one, updating
  the comment block at the top (lines 4–9) to describe the new count/list.

### `README.md`
This is the largest touchpoint by volume — the doc is currently organized
around the Render MCP feature. Target end state, section by section:

- **Title/intro (lines 1–9):** drop "pre-baked with Render tools" from the
  title and the `RENDER_MCP_API_KEY` risk callout. Optionally add one
  sentence noting that Render account access is deliberately not built
  into client instances — provisioning/managing Render resources is an
  admin-only action done outside this template (dashboard/CLI), not
  something a deployed agent instance can do. Worth keeping as a breadcrumb
  so a future reader doesn't wonder why this capability is conspicuously
  absent from an otherwise Render-savvy template.
- **Architecture diagram (lines 13–43):** remove the "registers Render MCP
  @ boot" / "calls mcp_render_* tools" / "Render MCP @ mcp.render" lines
  from the gateway box, and remove the `/opt/render-tools/skills-upstream`
  and `skills-local` boxes from the image-baked-layers box.
- **Lines 45, 51:** drop references to `render-oss/skills` bundle / "the
  bootstrap that registers the Render MCP server."
- **"What's pre-baked for Render" section (53–79):** delete entirely.
- **Prerequisites (81–100):** drop the "Render API key" optional bullet and
  the `RENDER_MCP_API_KEY`-specific warning callout (95–98). Keep the
  LLM-provider and Render-plan prerequisites, and the other optional
  chat-platform bullets.
- **Deploy walkthrough (102–116):** drop the `RENDER_MCP_API_KEY` prompt
  step (108) and its mention in the deploy-button flow.
- **"Post-deploy setup" (132–164):** drop the "Verify the Render tools are
  wired up" subsection (150–160) entirely, and the `RENDER_*` env var
  carve-out sentence in the dashboard/Environment-tab paragraph (148) — with
  the Render var gone, `HERMES_GATEWAY_TOKEN` is the only Blueprint-managed
  var and doesn't need the same carve-out.
- **"Updating" (205–230):** drop the `RENDER_SKILLS_REF` line from the
  build-args example (211) and the sentence about the skill bundle/overlay
  living under `/opt/render-tools/` (228).
- **Troubleshooting table (264–279):** remove the rows for `mcp_render_*`
  tools missing, `gosu: not found ... no Render MCP server`, `[render-tools]
  config patch failed`, and "Agent says it tried to run `render <something>`."
- **Security section (295–345):** this shrinks substantially since one of
  the two stated security surfaces goes away entirely. Remove the
  "Agent capabilities" subsection (304–309), "Why we don't ship the Render
  CLI" subsection (310–316), and the Render-API-key-scoping bullet under
  "Concrete steps to harden further" (320) along with the reference to the
  `render-on-hermes` overlay skill (323). Reframe the section intro
  (295–302) around the single remaining surface: dashboard auth exposing
  LLM provider keys. Keep "Dashboard access," Option A/B (auth
  gateway/Tailscale), and the closing notes (325–345) — all unrelated to
  Render MCP.
- **"What this template does and doesn't do" (368–387):** remove the
  `render-oss/skills` commit-pinning bullet, the skill-bundle/overlay
  bullet, the config-patching bullet, and the `RENDER_MCP_API_KEY sync:
  false` clause from the `HERMES_GATEWAY_TOKEN` bullet. Remove the "It
  doesn't install the `render` CLI" bullet under "doesn't do" (no longer a
  meaningful distinction once there's no Render MCP either) and the closing
  bullet about not forking `render-oss/skills` content.
- Leave untouched: Cost expectations, Chatting-with-the-agent, Shell
  access, "Open question: user identity through the API server", License.

### `plans/line-dm-pairing-plan.md`
- Not version-controlled and not part of this feature, but it has one
  stale reference (line 95) to "bump `RENDER_SKILLS_REF`/rebuild per the
  Dockerfile's existing upgrade path." Flag it, don't edit it as part of
  this change — it's a different, already-existing plan doc; leave a note
  there only if you revisit that plan later.

## Order of operations

1. Delete `scripts/bootstrap.sh`, `scripts/patch-config.py`,
   `tests/test_patch_config.py`, and `skills/render-on-hermes/SKILL.md`
   (then remove the now-empty `skills/` dir).
2. Edit `Dockerfile` to drop the three Render-tooling blocks and rewrite
   the header comment.
3. Edit `render.yaml` to drop the `RENDER_MCP_API_KEY` var and update
   comments.
4. Edit `.env.example` to drop the `RENDER_MCP_API_KEY` block.
5. Edit `scripts/smoke-test.sh` to drop the Render-specific assertion and
   env var, renumber remaining assertions.
6. Edit `README.md` per the section-by-section breakdown above.
7. Run `./scripts/smoke-test.sh` locally to confirm the image still builds
   and boots clean without the removed layers (this also answers the
   `/opt/data` pre-create open question — if the test's disk-dependent
   assertions still pass, the RUN line was either fine to keep or provably
   unnecessary; don't remove it speculatively either way without a second,
   deliberate pass).
8. Run `python -m pytest tests/` (or whatever the repo's test runner is)
   to confirm no leftover references to the deleted `patch-config` module.
9. `grep -ri` the whole repo one more time for `render_mcp`, `RENDER_SKILLS`,
   `skills-upstream`, `skills-local`, `render-on-hermes`, `mcp.render.com`
   to confirm nothing was missed.

## Verification

- `./scripts/smoke-test.sh` passes end to end (container builds, boots,
  stays up, gateway reaches `running`, Caddy routes correctly, dashboard
  auth gate is armed) with the two Render-specific assertions removed and
  everything else intact.
- `docker exec <container> cat /opt/data/config.yaml` (during a manual test
  run) shows no `mcp_servers.render` entry and no `skills.external_dirs`
  pointing at `/opt/render-tools/*` — confirms the boot no longer wires up
  Render MCP at all, not just that the skill text is gone.
- Final repo-wide grep (step 9 above) comes back empty.
