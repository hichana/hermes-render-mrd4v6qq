# Hermes Agent on Render

[Hermes Agent](https://github.com/NousResearch/hermes-agent) (the self-improving AI agent from Nous Research) on Render as a single Docker web service. We customize Hermes Agent as our harness for building out agent platforms for clients.

The initial instance was bootstrapped from [HERE](https://render.com/deploy-template/api/github/start?template_repo=hermes-render) but we've implemented a number of customizations to suit our use case. Our intention is to make our harness into a versioned template that we can use to deploy agent platforms for multiple clients.

In the repo, the `render.yaml` is the Blueprint describing our Render service. The Dockerfile defines a container image that Render builds and runs. Render provides and manages the VM/compute underneath, but we never interact with a VM directly. We only ever produce the image; Render's platform handles scheduling, restarts, and the runtime environment around the container.

The Hermes release is pinned in the `Dockerfile` for reproducible deploys. All Hermes state lives on a persistent disk so upgrades stay non-destructive, and the dashboard at the service URL is the primary setup surface.

> **Use at your own risk:** Hermes' dashboard holds your LLM provider keys and, with `HERMES_DASHBOARD_TUI=1`, a PTY into the container. Lock down dashboard access.

## Architecture

```
                            ┌──────────────────────────────────────────────┐
                            │ Render web service (Docker, plan: standard)  │
                            │                                              │
   you / external clients   │  ┌────────────────────────────────────────┐  │
   ─────────HTTPS──────────►│  │  hermes dashboard (s6 service, :10000) │  │
                            │  │  - /api/status (healthcheck, no auth)   │  │
                            │  │  - browser UI: config / keys / chat    │  │
                            │  │  - auth gate REQUIRED on 0.0.0.0 binds │  │
                            │  └────────────────────────────────────────┘  │
                            │                  │                           │
                            │  ┌────────────────────────────────────────┐  │
   Telegram / Discord /  ◄──┤  │  hermes gateway (s6: gateway-default)  │  │
   Slack / etc. (outbound)  │  │  - long-polls chat platforms           │  │
                            │  │  - spawns subagents per task           │  │
                            │  └────────────────────────────────────────┘  │
                            │                  │                           │
                            │                  ▼                           │
                            │  ┌────────────────────────────────────────┐  │
                            │  │  /opt/data (persistent disk, 5 GB)     │  │
                            │  │  .env, config.yaml, sessions/,         │  │
                            │  │  skills/, memories/, logs/             │  │
                            │  └────────────────────────────────────────┘  │
                            └──────────────────────────────────────────────┘
```

A single container runs both Hermes processes under [s6-overlay](https://github.com/just-containers/s6-overlay), which is the image's real init (`ENTRYPOINT ["/init", "/opt/hermes/docker/main-wrapper.sh"]`). The dashboard ([upstream docs](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/web-dashboard.md)) is an s6 service that runs whenever `HERMES_DASHBOARD=1` is set. The gateway runs as the container's main program (`CMD ["gateway", "run"]`) and registers itself as the supervised `gateway-default` service, so s6 restarts it if it crashes. Both share `/opt/data`.

> **Do not override the image's `ENTRYPOINT`.** Boot-time work belongs in an `/etc/cont-init.d/` hook. Overriding `ENTRYPOINT` is what broke the v2026.5.7 → v2026.7.7.2 upgrade: `/usr/bin/tini` is now a symlink to `/init`, so an old `tini -g -- ...` ENTRYPOINT resolved to `/init -g -- …`, s6 tried to run `-g` as the main program, and the boot failed — while the container stayed up and the health check kept passing. Run [`scripts/smoke-test.sh`](scripts/smoke-test.sh) after any `HERMES_IMAGE` bump.

Hermes also supports **one gateway process per profile**: each profile gets its own `/run/service/gateway-<name>` service, reconciled on every boot from that profile's `gateway_state.json` on the disk. Each runs with its own `HERMES_HOME` and its own `.env`, so per-profile settings (including port-binding platforms like LINE) stay independent. For our purposes, we will only run one agent/gateway per business. If a stray second profile ever does show up (e.g. from testing), see "[Deleting a profile safely](#deleting-a-profile-safely)" before removing it — `hermes profile delete` alone is not sufficient.

The disk holds everything that should survive a redeploy: API keys (`.env`), config (`config.yaml`), the FTS5 session database, installed skills, Honcho user models, agent memories, cron job definitions, and logs.

There's a single container filesystem here, not a VM with a separate OS inside it — Render runs one Linux container from the image this repo's `Dockerfile` builds, and everything you can interact with (Hermes, Caddy, s6, the dashboard) lives inside that one filesystem. Within it, the split that actually matters is ephemeral vs. persistent: `/opt/hermes` (the Hermes binary, its Python venv, static assets) comes from the image layers and resets to its baked-in state on every redeploy, while `/opt/data` (`HERMES_HOME`) is the mounted persistent disk and is the only part that survives across deploys. The dashboard's file browser is scoped to `/opt/data`, not the whole container — that's why you'll see agent-owned files like `SOUL.md` there but never `/opt/hermes`; reaching the image-baked side requires SSH (see [Shell access](#shell-access)).

## Prerequisites

Each agent needs:

- **An LLM provider API key.** [OpenRouter](https://openrouter.ai/keys) is the easiest because it routes to most providers behind a single key. Direct keys for Anthropic, OpenAI, Google, or Hugging Face also work.
- **A Render account** with at least the `standard` plan ($25/month at time of writing). The free plan can't run this image; the `standard` plan has the memory headroom Hermes needs.
- **A connection to a channel for Hermes to listen on**. Ex. Slack, Telegram, Line.

## Post-deploy setup

Once the service is healthy (the **Events** tab shows "Deploy live"), open the URL Render assigned (it ends in `.onrender.com`). We'll see the Hermes dashboard.

The Blueprint deliberately keeps the env-var surface tiny. All provider keys, tool keys, and chat platform tokens are set from the dashboard, not from `render.yaml`. The dashboard writes everything to `/opt/data/.env`, which lives on the persistent disk and survives redeploys.

Walk through these tabs in order:

1. **API Keys**. Paste a key for at least one LLM provider. Pick one:
2. **Config**. Set the `model` field at the top of the list. 
3. **Status**. Confirm the gateway is running and the model is reachable. The "Connected platforms" list will be empty until we add a chat platform.
4. **API Keys** 

If we'd rather set keys from the Render Dashboard's **Environment** tab (handy for CI or secrets-manager workflows), that mostly also works — but confirmed in practice (2026-07-23) that it's not equivalent for everything: Hermes' own skill-readiness checks (e.g. the `line-invite` skill's `LINE_BASIC_ID` requirement, see below) read `/opt/data/.env` first and don't reliably fall back to a Render-Environment-tab-only value. For anything a skill or plugin declares as a required/optional env var, set it from the dashboard, not the Environment tab.

### LINE Basic ID (per instance)

If this instance uses the `line-invite` skill (manager-initiated QR join invites), it needs `LINE_BASIC_ID` — the channel's public LINE Basic ID (LINE Developers Console → Messaging API → Basic Settings, e.g. `@abc1234`) — set in the dashboard's **API Keys** tab. It's not a secret, just per-instance config, but it has to land in `/opt/data/.env` the same way provider keys do; see the note above for why the Environment tab alone isn't enough.

To automate this per new client instance instead of visiting the dashboard by hand: set `LINE_BASIC_ID` as a plain Render **Environment**-tab var when you provision the service. A boot-time hook (`scripts/seed-env-from-render.py`, installed via the Dockerfile) copies it into `/opt/data/.env` automatically on first boot — idempotent, and it never overwrites a value you later change from the dashboard. See the Dockerfile's `line-invite skill` comment block for how this is wired up.

### Where the "gateway token" fits

The Blueprint generates a `HERMES_GATEWAY_TOKEN` for you. Today, upstream Hermes doesn't read this variable directly at runtime: it's a placeholder for the OpenAI-compatible API server's bearer key. If you opt into the API server (set `API_SERVER_ENABLED=true` from the dashboard's **API Keys** tab, then paste this token into `API_SERVER_KEY`), external HTTP clients can authenticate against `/v1/chat/completions` using `Authorization: Bearer <that value>`.

## Chatting with the agent

The simplest way to talk to your deployed Hermes is the dashboard's **Chat** tab. The Blueprint sets `HERMES_DASHBOARD_TUI=1`, which makes the upstream dashboard expose the full TUI in the browser over a server-side PTY plus xterm.js. Slash commands, model picker, tool-call cards, streaming, sessions: everything works the same as a local terminal.

If you'd rather stay on the command line, two paths work, both because the in-container `hermes` is the same binary as the local CLI:

- **One-shot prompts via Render Shell or SSH.** The browser shell on Render does not allocate a TTY for `runtime: image` services. The interactive REPL (`hermes` with no args) will print a banner and quit immediately with `Warning: Input is not a terminal (fd=0)`. Use the non-interactive form instead:

  ```bash
  /opt/hermes/.venv/bin/hermes chat -q "summarize today's logs"
  ```

  This runs one turn, prints the result, and exits cleanly. You can chain it with `--resume <session-id>` to continue an existing conversation.

- **Real terminal via the Render CLI.** From your local machine:

  ```bash
  render ssh <service-id>
  /opt/hermes/.venv/bin/hermes
  ```

  `render ssh` allocates a PTY, so the interactive REPL works.

The chat tab in the dashboard is still the cleanest UX. Use the CLI fallbacks when you're scripting or already in a terminal context.

## Cost expectations

Costs assume Render's published prices in May 2026 and don't include data egress, which is unmetered for typical Hermes traffic.

| Component                     | Plan                              | Cost            |
|-------------------------------|-----------------------------------|-----------------|
| Web service (`runtime: image`) | `standard` (2 GB / 1 CPU)         | $25/month       |
| Persistent disk (`/opt/data`)  | 5 GB SSD                          | $1.25/month     |
| **Subtotal (this template)**   |                                   | **$26.25/month**|

If you do a lot of Playwright browsing or run several subagents in parallel, bump the plan to `pro` (4 GB / 2 CPU, $85/month). The starter plan (512 MB) cannot hold the Hermes image and is not supported.

LLM costs are separate and depend entirely on your provider and usage. OpenRouter and Anthropic both report usage in their respective dashboards; Hermes also surfaces per-model usage on its **Analytics** page.

## Updating

The pinned Hermes version lives in the [`Dockerfile`](Dockerfile) as a build arg:

```dockerfile
ARG HERMES_IMAGE=docker.io/nousresearch/hermes-agent:v2026.?.?.?
```

Bump it, then **run the smoke test before you deploy**:

```bash
./scripts/smoke-test.sh
```

It builds the image, boots it the way Render does, and asserts the container stays up and the gateway reaches `running`. This is not ceremony: the v2026.5.7 → v2026.7.7.2 bump broke the boot three separate ways at once (s6-overlay migration, `tini` → `/init` symlink, `gosu` removed), and *none* of them surfaced as an obvious failure — the container stayed up wedged mid-shutdown, the dashboard kept answering the health check, and Render marked the deploy live. It ran that way for 8 days. Upstream ships roughly weekly releases with ~180 commits each, so assume every bump can move the ground under the image.

Then commit and push. Render won't auto-deploy (the Blueprint sets `autoDeployTrigger: off`); trigger a manual deploy from the Dashboard or the [Render CLI](https://render.com/docs/cli) on your own machine:

```bash
render deploys create <service-id>
```

Your `/opt/data` disk is untouched across image upgrades. The upstream entrypoint runs a manifest-based `skills_sync.py` on each boot, which preserves edits to bundled Hermes skills.

Hermes ships fast: roughly weekly tagged releases, each with around 180 commits. Check [the upstream releases page](https://github.com/NousResearch/hermes-agent/releases) before bumping `HERMES_IMAGE`.

## Troubleshooting

### Logs

Render keeps logs in the **Logs** tab of your service. Filter by stream:

- The dashboard side-process prefixes its lines with `[dashboard]`.
- Gateway and agent logs are unprefixed.
- For deeper inspection, log files also live on disk at `/opt/data/logs/` (`agent.log`, `errors.log`, `gateway.log`).

You can tail them from the dashboard's **Logs** tab too, or via SSH (next section).

### Shell access

Render gives you SSH into the container. From the service's overview page, click **Shell** (browser PTY) or copy the SSH command from **Settings**.

```bash
# Inspect the data volume.
ls /opt/data
cat /opt/data/.env

# Run the Hermes CLI directly.
/opt/hermes/.venv/bin/hermes status
/opt/hermes/.venv/bin/hermes config get model.default
```

The container runs as the `hermes` user (UID 10000), not root.

### Service won't start

Check the **Events** tab for the deploy that failed, then the **Logs** tab around that timestamp.

| Symptom                                              | Likely cause                                                                 |
|------------------------------------------------------|------------------------------------------------------------------------------|
| `Refusing to start: binding to 0.0.0.0 requires API_SERVER_KEY` | You set `API_SERVER_ENABLED=true` and `API_SERVER_HOST=0.0.0.0` without an `API_SERVER_KEY`. Set the key or flip back to `127.0.0.1`. |
| Health check fails on `/api/status`                  | `HERMES_DASHBOARD` is unset or the dashboard crashed. Check `[dashboard]` lines for a Python traceback. |
| Container OOM-killed                                 | Bump plan to `pro`. Playwright/Chromium is the usual culprit.                 |
| `Permission denied` on `/opt/data/...`               | The disk was attached after a deploy that ran as a different UID. Restart the service; the entrypoint chowns `/opt/data` on boot when run as root. |
| `Warning: Input is not a terminal (fd=0)` then `Goodbye!` when running `hermes` | Render's browser shell pipes stdin instead of allocating a PTY. Chat from the dashboard's **Chat** tab, or use `hermes chat -q "..."`, or `render ssh <service-id>` from a local terminal. |
| `-g: not found` / `rc.init: 91:` in the boot logs, then the service behaves erratically | The image's `ENTRYPOINT` was overridden. `/usr/bin/tini` is a symlink to `/init`, so any `tini …` ENTRYPOINT becomes `/init …` and s6 runs the leftover flags as the main program. Remove the override — the image's own `ENTRYPOINT` (`/init` + `main-wrapper.sh`) plus `CMD ["gateway", "run"]` is correct. Put boot work in `/etc/cont-init.d/`. |
| Service looks healthy but `ps` shows `s6-rc -bda change` running for days | The container is **wedged mid-shutdown**: the boot failed, s6 started tearing down, and the teardown hung waiting on a dashboard process that never exited. The dashboard keeps answering `/api/status`, so Render's health check passes and the deploy is marked live. It cannot survive a restart. Fix the boot (row above) and redeploy. |
| `Refusing to bind dashboard to 0.0.0.0 — … no auth providers are registered` | Expected, and it fails closed. Set `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` + `_PASSWORD` from the Environment tab. |
| Dashboard **Chat** tab shows "Chat unavailable: 1" or hangs / 500s on `/api/pty` | `/opt/hermes/ui-tui/` and `node_modules/` still ship root-owned while the dashboard runs as `hermes` ([#20500](https://github.com/NousResearch/hermes-agent/issues/20500)), so a runtime esbuild rebuild fails with `EACCES`. The Dockerfile chowns them at build time; if you've forked and removed that line, restore it. (The old `touch ink-bundle.js` / `entry.js` workarounds are no longer needed as of v2026.7.7.2: `_hermes_ink_bundle_stale()` and `_tui_build_needed()` are gone, and the image ships a prebuilt `ui-tui/dist/entry.js`.) |
| `tirith security scanner enabled but not available`  | Harmless. Tirith is an optional Rust-based command scanner; without it, Hermes uses pattern matching. Ignore unless you specifically want native scanning. |
| Two profiles both trying to run the same port-binding platform (e.g. LINE), one stuck retrying `bind_failed` forever, or a deleted profile's directory reappears after a restart | See "[Deleting a profile safely](#deleting-a-profile-safely)" below. |

### Deleting a profile safely

**Confirmed in practice (2026-07-24): `hermes profile delete <name>` does not reliably kill the profile's running gateway process.** It removes the profile's files and updates Hermes' own bookkeeping (`hermes profile list` will report it "stopped" or gone entirely), but the underlying OS process can keep running. On Render, you can't clean up after this the way you would locally — SSH access there runs as UID 0 but **without `CAP_KILL`** into the main container's process tree, so `kill`/`os.kill()` against the orphaned PID fails with `PermissionError: Operation not permitted` even as root, and `s6-svc -d` fails with "No such file or directory" if the service's control path was already torn down by the deletion.

Left alone, that orphaned process is actively harmful, not just idle: through its own normal housekeeping writes (session state, logs, `gateway_state.json`), it will **recreate its own profile directory** on disk within minutes. The next container restart's boot-time reconciler (`hermes_cli/container_boot.py`, wired as `/etc/cont-init.d/02-reconcile-profiles`) walks every directory under `$HERMES_HOME/profiles/` and re-registers an s6 service for anything with a `SOUL.md` — so it will faithfully resurrect a `gateway-<name>` service for a profile you already "deleted."

Safe procedure:

1. **Stop the gateway first, and verify it's actually gone**, before deleting anything:
   ```bash
   /opt/hermes/.venv/bin/hermes -p <name> gateway stop
   ps aux | grep "hermes -p <name>"   # must print nothing
   ```
2. Only then run `hermes profile delete <name> --yes`.
3. If step 1's `ps` check ever does show a lingering process after `gateway stop` (or you skip straight to `delete` and find one afterward) — don't fight it over SSH. **Restart the service from the Render Dashboard** (or `render deploys create` a no-op redeploy). This is the only reliable way to clear an orphaned gateway process on Render; the container's tmpfs `/run/service/` is wiped on restart, and the reconciler will not recreate a slot for a profile whose directory is genuinely gone.
4. After the restart, confirm with `hermes profile list` that only the profiles you expect are present, and re-run step 2's deletion again if the directory got resurrected in the gap before the restart.

If you want the conversation history a deleted profile was holding (sessions, memories), get it out *before* deleting:

```bash
hermes profile export <name>                              # full backup archive, cheap insurance
hermes -p <name> sessions export --format md \
  --session-id <id> <output-dir> --yes                     # human-readable transcript
```
Memory files (`memories/USER.md`, `memories/MEMORY.md`) are just markdown — worth diffing against the profile you're keeping and merging anything genuinely useful (not boilerplate carried over from a `--clone`) before the source profile is gone.

### Changing env vars

Set, change, or delete env vars under the service's **Environment** tab. Render restarts the container after a save. Hermes also exposes a `/reload` slash command for in-session reloads if you've already started chatting from the CLI; it's not relevant for the gateway, which restarts cleanly.

### Forcing a clean rebuild

If the Hermes data directory gets into a bad state (corrupt session DB, partial skill install), wipe it:

1. SSH in.
2. `mv /opt/data /opt/data.bak && exit`.
3. Restart the service from the Render Dashboard. The entrypoint recreates the directory tree and reseeds defaults.

Or restore the most recent automatic disk snapshot from the **Disks** page.

## Security

Hermes' web dashboard has no built-in authentication of its own (the Blueprint arms one via `HERMES_DASHBOARD_BASIC_AUTH_*`, see "Protect the URL before configuring"). Anyone who reaches an unlocked dashboard can read your LLM provider keys, change configuration, and chat with the agent — with `HERMES_DASHBOARD_TUI=1`, that includes a PTY into the container. This template deliberately does not give the agent any Render account access (no MCP server, no API key, no CLI), so the blast radius of an exposed dashboard stops at this one service — it can't reach into your broader Render account. The dashboard lock is still on you.

### Dashboard access

Two practical options.

#### Option A: Auth gateway

Expose a small authenticated Web Service in front of Hermes and keep Hermes itself private. The gateway verifies a bearer token, OAuth session, or identity-provider token, then forwards approved traffic to Hermes over Render's private network.

This is the most portable option because it does not depend on static client IPs.

#### Option B: Tailscale

Skip the public internet entirely. Run Tailscale on a sidecar (or use Render's [Tailscale template](https://render.com/docs/deploy-tailscale-derp)) and reach the dashboard only from devices on your Tailnet. This takes more setup, but it avoids IP rotation pain and works from anywhere.

#### Notes

- These options compose. For example, an auth gateway can still sit behind a private network path.
- The OpenAI-compatible API server (`API_SERVER_ENABLED=true`) is separate from the dashboard. It uses a bearer token (`API_SERVER_KEY`), so it's safe to expose with a long random key, but this Blueprint doesn't route it publicly.
- For broader Hermes security guidance see the [upstream security doc](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/security.md).

## Open question: user identity through the API server

Relevant if you plan to build your own UI (a customer-facing dashboard, say) against Hermes' OpenAI-compatible API server rather than reaching the agent through a chat platform. **Unresolved — verify before you depend on it.**

The two doors into the agent do not carry identity the same way:

| | Chat platform (LINE, Slack, …) | API server (`/v1/chat/completions`) |
|---|---|---|
| Who the user is | Derived from the platform's signed payload (e.g. LINE's HMAC-verified webhook → real user ID) | **Asserted by the caller** |
| Session scoping | Gateway keys it: `agent:main:<platform>:dm:<chat_id>` | `X-Hermes-Session-Key` header, or it collapses |
| Honcho peer | `userPeerAliases` maps the platform user ID → a distinct peer | **Unknown — see below** |
| Trust boundary | Hermes verifies the platform's signature | Hermes trusts your app completely |

By default the API server stamps requests with a single shared channel (`"chat_id": "api"`) — there's no per-user concept. To scope memory per user you must send `X-Hermes-Session-Key`, documented in `gateway/platforms/api_server.py` as *"a stable per-channel identifier that scopes long-term memory (e.g. Honcho sessions) across transcripts."* It requires `API_SERVER_KEY` — the source is explicit that accepting a caller-supplied memory scope without authentication would let a client *"inject itself into another user's long-term memory scope by guessing a key."*

**What we could not establish:** `userPeerAliases` maps *platform runtime IDs* to Honcho peers, and the API path has no platform user ID. So whether a session key yields a distinct Honcho **peer** — or merely a distinct session under one blended peer — is unverified. That's the difference between real per-user modeling and per-user transcripts, and it's precisely the blending failure Honcho's peer model exists to prevent.

**To resolve:** point a test instance at a real Honcho workspace, drive two conversations through `/v1/chat/completions` with different `X-Hermes-Session-Key` values, and inspect whether Honcho records one peer or two.

**Consequence either way:** your app becomes the sole guarantor of user isolation on that path, since the session key is caller-supplied. That's a fair trade — it's also what lets you implement the per-user RBAC Hermes doesn't have — but it must be deliberate.

## What this template does and doesn't do

What it does:

- Pins a specific upstream Hermes image for reproducible deploys.
- Runs the Hermes gateway and dashboard inside one container, the way upstream supports.
- Mounts a persistent disk at the upstream-default `HERMES_HOME` path.
- Generates a `HERMES_GATEWAY_TOKEN` so secrets never sync from the repo.
- Sets a healthcheck that probes the dashboard.

What it deliberately doesn't do:

- **It doesn't give the agent any Render account access.** No Render MCP server, no Render API key, no `render` CLI. Provisioning and managing Render resources is an admin-only action taken outside the deployed agent.
- It doesn't try to add authentication on top of the dashboard. Use an auth gateway, private network path, or another access-control layer you trust.
- It doesn't enable the OpenAI-compatible API server. Flip `API_SERVER_ENABLED=true` and supply `API_SERVER_KEY` if you need it.
- It doesn't ship a default model. Hermes' upstream default is set in `config.yaml`, which lives on disk and is owner-configurable from the dashboard.
- It doesn't configure browser automation tweaks (`--shm-size`, GPU access). Those need an instance type with more RAM, not extra Render config.

## License

This template is MIT licensed (see [`LICENSE`](./LICENSE)). Hermes Agent itself is also MIT licensed; see [the upstream LICENSE](https://github.com/NousResearch/hermes-agent/blob/main/LICENSE).
