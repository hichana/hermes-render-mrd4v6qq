# CLAUDE.md — hermes-render

See @SERVICES.md for connecting to admin services like SSH for accessing a Render service.

See @PACKAGING.md for details on how a client service will be packaged for deployment.

## About NGraph

NGraph is a company that focuses on bespoke AI integrations for businesses based in Fukui and Ishikawa prefectures, Japan. Our broader goal is to develop a SaaS offering spawned from our experience building solutions for businesses and supplant our integrations business with one that is more scaleable. 

NGraph members:
Singo Takahashi -- CEO
Matt Chana -- CTO

## What this repo is

A Docker template for deploying one [Hermes Agent](https://github.com/NousResearch/hermes-agent) instance per client business on Render. Each deployed instance is client-facing. **Only admins provision or manage Render resources** — never a deployed agent instance. Each image should carry no Render account access at all: no MCP server, no Render API key, no `render` CLI.

### Env-var allowlist edits vs. pairing-store approvals — different reload rules

Two mechanisms gate LINE (and other platform) DM access, and they do NOT reload the same way:

- **`{PLATFORM}_ALLOWED_USERS` / `_DM_POLICY` / `_ALLOW_ALL_USERS` env vars** are read once via `os.getenv()` at adapter construction (e.g. `LineAdapter.__init__` → `self.allowed_users`) and then cached for the process's lifetime. Editing `/opt/data/.env` does **nothing** to an already-running gateway — it requires an actual process restart to take effect. Use `admin-tools/env-sync push <slug>` for this (see its README) — it does the upsert into `/opt/data/.env` and the restart-and-verify in one step. Manual SSH edit followed by a separate manual restart is no longer the sanctioned path.

- **Pairing-store approvals** (`hermes pairing approve/revoke`, or invite-token redemption via the `line-invite` skill) go through `PairingStore.is_approved()`, which re-reads its JSON file from disk on *every message* — no caching. These take effect immediately, never need a restart, and are the right tool for a quick revoke/re-grant test loop.

- **Group response mode** (`/opt/data/platforms/line-modes/modes.json`, written by `GroupModeStore` in `modules/line/render_mention.py`) is a *third* mechanism, and it sits on the **no-restart** side with the pairing store, not with the env vars. `get_mode()` re-reads the file on every group message, deliberately, so the in-chat toggle (the Template Buttons bubble, or `mode always` / `mode mention` addressed to the bot) takes effect on the very next message. `LINE_REQUIRE_MENTION` and `LINE_MENTION_FOLLOWUP_SECONDS` are env vars and therefore *do* follow the restart rules above — but they only supply the default for a group the store has never seen. A stored mode always wins, so don't reach for `env-sync push` to change one group's behavior.

**Don't trust a "restart requested" log line as proof a restart happened.** Confirmed live (2026-07-27): `hermes gateway restart` is a **silent no-op** on this deployment — the gateway runs as the container's bare main process (started by `main-wrapper.sh`, never through `hermes gateway install`), so `hermes gateway status` reports "Running manually, not as a system service," and the CLI's restart subcommand has no registered service to dispatch to. It exits 0 with zero output and changes nothing. The dashboard's own restart button (`POST /api/gateway/restart`) shells out to this identical subcommand, so it shares the bug — not a usable alternative either.

What does work: sending `kill -USR1 <pid>` directly to the gateway's own pid — but **only as the `hermes` user**, not root. Root gets `Operation not permitted` even though it can freely `ps` the process: Render's runtime drops `CAP_KILL`, and `kill()` requires either a UID match or that capability — self-signaling as the owning user is always allowed regardless. As `hermes` (e.g. `/command/s6-setuidgid hermes kill -USR1 <pid>` — note `/command/s6-setuidgid`'s full path; it's not on an interactive SSH/Shell session's `PATH`), this is wired in `gateway/run.py` to `request_restart(via_service=True)`, which drains in-flight agent runs (up to `agent.restart_drain_timeout`, default 180s) then exits; the container's own s6 supervision relaunches it, producing a new pid. On an idle instance this completed in under 20s in testing — nowhere near the 180s ceiling — but don't assume that always holds under load.

Either way, verify the same way: `cat /opt/data/gateway.pid` (compare `pid`/`start_time` before and after — both must differ) or `ps -o lstart -p <pid>` — if the PID is unchanged, the restart didn't happen. For an env-var change specifically, confirm in `logs/gateway.log` that the sender you removed actually gets an `Unauthorized user: <id>` line on their next message — that's ground truth, not the restart-requested log line.

### Access is the union of both stores — `.env` alone won't show you who's in

A user is authorized if they're in **either** `{PLATFORM}_ALLOWED_USERS` (env) **or** the pairing store (`line-approved.json`) — it's an OR, not one superseding the other. Concretely: Render's dashboard Shell/`.env` view only shows the env-var side. Someone can have working chat access purely via a redeemed invite code or an operator `pairing approve`, and never appear in `.env` at all. So "who currently has LINE access" always requires checking both `/opt/data/.env` (`LINE_ALLOWED_USERS`) *and* `/opt/data/platforms/pairing/line-approved.json` over SSH — never trust the dashboard's env view alone as the full picture. If `clients/<slug>.env` (see `admin-tools/env-sync`) has actually been kept in sync — i.e. every `push` since the last manual edit — it's a quick local check for the env-var side without SSHing in at all; the pairing-store side has no local mirror and always requires SSH regardless.

### Removing a user's access

Check both stores first (see above) to know which one(s) actually grant this user access, then remove from each accordingly:

- **If they're in `line-approved.json` (pairing store):** run `hermes pairing revoke <user_id>` (or edit the JSON directly, same backup-then-edit-then-confirm-then-delete-backup discipline as any other manual edit to this file). Takes effect immediately, on their very next message — no restart needed, since `is_approved()` reads the file fresh every time.
- **If they're in `LINE_ALLOWED_USERS` (env):** edit `clients/<slug>.env` to remove their ID, then run `hermes-env-sync push <slug>` (see `admin-tools/env-sync`) — it upserts `/opt/data/.env` and restarts-and-verifies the gateway in one step. Confirm the tool reports restart-verified, then confirm an `Unauthorized user: <id>` line for them in `logs/gateway.log` on their next message — that's ground truth, not the tool's own "verified" print. An edit alone does nothing until a real, verified restart happens — this is the exact failure mode this section exists to guard against.
- If they're in **both**, do both steps — removing only one leaves them authorized via the other.

## Reusable patterns

We built and then deliberately removed a Render-specific MCP integration. The integration was Render-specific, but the *mechanisms* it used are generic and worth reusing the next time this repo needs to bake in a new capability and wire it up at boot. Rather than leave that code sitting around unused as a template, it's captured here — pull the actual implementation from git history (`git show 3eb2be3:<path>`) as a starting point rather than reinventing it.

### Pattern 1: boot-time idempotent config mutation

**Problem:** you need to add something to Hermes' `~/.hermes/config.yaml` (e.g. an MCP server entry, a new `skills.external_dirs` path) at container boot, without ever overwriting an operator's manual edits to that file.

**Shape of the solution** (was `scripts/patch-config.py`, `git show 3eb2be3:scripts/patch-config.py`):
- A small Python script (PyYAML ships in Hermes' own `.venv`, so no extra deps) that loads `config.yaml`, checks whether the key you want already exists, and **only inserts if missing** — never overwrites, never deletes. This makes it safe to run on every single boot.
- Writes via a temp file + `Path.replace()` (atomic rename), not an in-place edit, so a crash mid-write can't corrupt the file.
- Takes the config path as a CLI arg rather than hardcoding it, so it's testable without touching a real `~/.hermes/config.yaml`.
- Exits 0 even on most failure paths (bad YAML, unwritable file) and logs a `[your-prefix]` prefixed warning to stderr instead — a boot-time patcher should never be the reason the container fails to come up.

### Pattern 2: running it at boot via a cont-init hook

**Shape of the solution** (was `scripts/bootstrap.sh`, `git show 3eb2be3:scripts/bootstrap.sh`, installed by the `Dockerfile` at `git show 3eb2be3:Dockerfile`):
- Installed as `/etc/cont-init.d/NN-<name>` (s6-overlay convention — hooks run in lexical order). Upstream Hermes ships hooks numbered `01`, `015`, `02`; pick a number that lands after whatever your hook depends on (we used `03`, after the volume was chowned and seeded).
- Shebang is `#!/command/with-contenv sh`, **not** plain `#!/bin/sh`, if the hook (or anything it execs, e.g. via `s6-setuidgid`) needs to read a Render/docker `-e` env var. s6-overlay v3 cont-init scripts don't get the container environment for free — it's captured into `/run/s6/container_environment/` files at container start, but only `with-contenv` actually exports those into a script's process env. A real incident: `scripts/seed-env-from-render.py` (Pattern 1 applied to `.env`) silently saw an empty value for the var it was supposed to seed, because `bootstrap.sh` used plain `#!/bin/sh` — `docker exec -u hermes <script>` found the var fine (exec goes through a different path), which is what made it look like a script bug at first rather than a shebang/environment-propagation one. Plain `#!/bin/sh` is still correct for hooks that only touch `config.yaml`/static paths and never read a caller-supplied env var.
- `set -eu`, run as root (cont-init always does), and privilege-drop into `hermes` for anything that touches `/opt/data` via `s6-setuidgid` — **not** `gosu`, which isn't present in this s6-based image (a real incident: see `README.md`'s "Service won't start" table history for the v2026.5.7 → v2026.7.7.2 upgrade).
- Never `exec`s anything — a hook that execs pre-empts s6 from starting the rest of the supervised services.
- Failures inside the hook are logged and swallowed, not fatal — same reasoning as the patcher itself: this is additive functionality, and its absence should degrade gracefully, not take down the agent.

### Pattern 3: adding a skill without forking upstream

**Shape of the solution** (was `skills/<name>/SKILL.md`, wired via `skills.external_dirs` in the patched `config.yaml`):
- Drop a new `skills/<skill-name>/SKILL.md` in this repo; the `Dockerfile` `COPY`s the whole `skills/` dir into an image layer (we used `/opt/render-tools/skills-local`), and the boot-time patcher (Pattern 1) registers that path under `skills.external_dirs`.
- If you're also pulling in an upstream skill bundle from another repo (we did, from `render-oss/skills`, pinned at a commit ARG), list your own overlay directory **first** in `external_dirs` — Hermes resolves same-named skills by first-match, so your overlay shadows anything with a colliding name in the upstream bundle.
- Remember: a skill is guidance text the agent may or may not load based on its `description` matching the request — it is never an access control mechanism. If the skill exists to explain a capability (an MCP server, an API key), gate the capability itself (Pattern 1), not just the explanation of it.

### Pattern 4: boot smoke test — assert real state, not log lines

**Shape of the solution** (`scripts/smoke-test.sh`):
- Build the image, boot it with `docker run` flags that mirror Render's actual runtime constraints (`--security-opt no-new-privileges`, `--cap-drop NET_BIND_SERVICE` — Render is stricter than stock Docker, and this caught a real prod-only failure before it shipped).
- For each thing you baked in, assert against the **live artifact**, not a log message: e.g. `docker exec <container> grep -q "<marker>" /opt/data/config.yaml`, not `docker logs | grep "patched successfully"`. A failed privilege drop or a swallowed exception can still print a success-shaped log line while leaving the actual file unchanged — this bit us for real (the `gosu` removal, 8 days of a wedged-but-"healthy" container).
- Run this before every `HERMES_IMAGE` bump, not just when you touch this repo's own code. Upstream ships ~180 commits a week; assume every bump can move the ground under the image.

### Dockerfile / render.yaml conventions worth keeping

- Pin external things by commit/tag via `ARG`, with the pin and the "how to upgrade" instructions living next to each other in a header comment (see current `Dockerfile` header, and the "Updating" section of `README.md`).
- Anything that must never sync from a Blueprint (secrets, per-service values) goes in `render.yaml` as `sync: false`, with a comment explaining why it's sensitive and where to generate it.
- Don't override the image's `ENTRYPOINT`. Put boot-time work in `/etc/cont-init.d/` hooks instead (see Pattern 2) — the `Dockerfile`'s closing comment block explains exactly why this matters and what broke when it wasn't followed.

## Validation

- `./scripts/smoke-test.sh` — builds and boots the image the way Render does, then asserts it stays up, the gateway reaches `running`, and (currently) Caddy routes and the dashboard auth gate is armed. Extend this file's assertions rather than adding a separate test runner for anything that only matters at boot.
- `tests/` doesn't currently exist (it held one file, testing Pattern 1's patcher, and was removed along with it) — recreate it for anything unit-testable without a running container. The removed test imported its module directly via `importlib` rather than a package import (`git show 3eb2be3:tests/test_patch_config.py`), which is a reasonable pattern for testing a standalone `scripts/*.py` file without needing it installed as a package.
