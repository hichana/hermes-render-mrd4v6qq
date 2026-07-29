# CLAUDE.md — hermes-render-mrd4v6qq

See @SERVICES.md for connecting to admin services like SSH for accessing a Render service.

## About NGraph

NGraph is a company that focuses on bespoke AI integrations for businesses primarily based in Fukui, Japan, tho strive to address a much wider market. Our broader goal is to develop a SaaS offering spawned from our experience building solutions for businesses and supplant our integrations business with one that is more scaleable. 

NGraph members:
Singo Takahashi -- CEO
Matt Chana -- CTO

## What this repo is

A Docker template for deploying one [Hermes Agent](https://github.com/NousResearch/hermes-agent) instance per client business on Render. Each deployed instance is client-facing. **Only admins provision or manage Render resources** — never a deployed agent instance. 

## How env vars work here

### The three places a value can come from, and which one wins

1. **`render.yaml` (the Blueprint).** Deliberately tiny — only vars that change how the *container boots* (`HERMES_DASHBOARD*`, `HERMES_GATEWAY_TOKEN`). Anything secret or per-service is declared `sync: false`, meaning the Blueprint names the key but never carries its value; it's set once per service from Render's Environment tab and a Blueprint sync will not overwrite it.
2. **Render's Environment tab.** Injected into the container's process environment at start. Saving here restarts the container.
3. **`/opt/data/.env`** on the persistent disk (`HERMES_HOME`). Survives redeploys. Written by Hermes' own dashboard (API Keys tab), and by `admin-tools/env-sync`.

**`.env` beats Render, always.** Hermes loads `$HERMES_HOME/.env` with `override=True` at gateway startup, so for any key that file contains — *including one with a blank value* — the Render-injected value is discarded. This is the single most important gotcha here: a key can be visibly correct in Render's Environment tab and have no effect whatsoever, silently, because a stale line for it exists in `.env`. (Note: `.env.example`'s closing paragraph currently states the precedence backwards; `README.md` and `admin-tools/env-sync/README.md` have it right.)

Practical consequence: **treat `/opt/data/.env` as the source of truth for everything except container-boot config**, and don't hand-edit the Render tab for business config at all.

### Historical: the boot-time seeder is gone

There used to be a `scripts/seed-env-from-render.py` cont-init step that copied a couple of Render Environment-tab vars into `.env` on boot. It was **insert-only** (never overwrote), so it worked exactly once per key and then went permanently, silently inert — edit the Render tab afterwards and nothing happened. It was removed in favor of `env-sync`. Don't reintroduce a boot-time seeder for this.

One live incident from it is still worth carrying forward: cont-init hooks under s6-overlay v3 do **not** see Render/docker `-e` env vars unless the shebang is `#!/command/with-contenv sh`. The seeder saw an empty value for its var purely because `bootstrap.sh` used plain `#!/bin/sh`. See Pattern 2 below.

### Changing a client's env vars: `admin-tools/env-sync`

The sanctioned path for every ongoing change — rotating a key, onboarding LINE credentials, editing an allowlist — is [`admin-tools/env-sync`](admin-tools/env-sync/README.md), an admin-machine-only CLI. Not the Hermes dashboard, not the Render tab, not a hand edit over SSH.

- `clients/<slug>.env` is the local source of truth for the keys **it** manages; `clients/registry.yaml` maps slug → Render SSH target. Both are gitignored (real secrets), as is `.backups/`.
- Workflow is: edit `clients/<slug>.env`, run `hermes-env-sync push <slug>`, read the printed diff, confirm.
- It's a **targeted upsert**, not a file replacement: keys absent from your local file (e.g. anything set by hand through Hermes' dashboard) are left byte-for-byte untouched in place. That's why **deleting a line locally does not delete it remotely** — write a `!KEY_NAME` bang marker instead to actually remove a key.
- `push` also backs up the pre-write remote `.env` to `.backups/`, restarts the gateway, verifies the restart really happened (below), and appends a row to `push-log.csv`. That CSV is **committed on purpose** — key names only, never values — and is the only record in git of when a live client instance last changed. Commit it alongside whatever prompted the push.
- `diff <slug>` is read-only; `push --dry-run` previews; `restart-only <slug>` when `.env` is already right.

### Env-var changes need a *verified* restart

Hermes reads env vars once, via `os.getenv()`, at process start / adapter construction (e.g. `LineAdapter.__init__` → `self.allowed_users`), then caches them for the process's lifetime. Editing `/opt/data/.env` does **nothing** to a running gateway.

**Don't trust a "restart requested" log line as proof a restart happened.** Confirmed live (2026-07-27): `hermes gateway restart` is a **silent no-op** on this deployment — the gateway runs as the container's bare main process (started by `main-wrapper.sh`, never through `hermes gateway install`), so `hermes gateway status` reports "Running manually, not as a system service," and the CLI's restart subcommand has no registered service to dispatch to. It exits 0 with zero output and changes nothing. The dashboard's own restart button (`POST /api/gateway/restart`) shells out to this identical subcommand, so it shares the bug — not a usable alternative either.

What does work: sending `kill -USR1 <pid>` directly to the gateway's own pid — but **only as the `hermes` user**, not root. Root gets `Operation not permitted` even though it can freely `ps` the process: Render's runtime drops `CAP_KILL`, and `kill()` requires either a UID match or that capability — self-signaling as the owning user is always allowed regardless. As `hermes` (e.g. `/command/s6-setuidgid hermes kill -USR1 <pid>` — note `/command/s6-setuidgid`'s full path; it's not on an interactive SSH/Shell session's `PATH`), this is wired in `gateway/run.py` to `request_restart(via_service=True)`, which drains in-flight agent runs (up to `agent.restart_drain_timeout`, default 180s) then exits; the container's own s6 supervision relaunches it, producing a new pid. On an idle instance this completed in under 20s in testing — nowhere near the 180s ceiling — but don't assume that always holds under load.

`env-sync push` does exactly this and then polls for proof, so you normally never do it by hand. Verify the same way it does: `cat /opt/data/gateway.pid` — **both** `pid` and `start_time` must differ from before — or `ps -o lstart -p <pid>`. Unchanged pid means no restart, no matter what any log line or exit code said. Where a behavioral check exists, prefer it as final ground truth (for an allowlist removal: an `Unauthorized user: <id>` line in `logs/gateway.log` on that sender's next message).

Note that a Render redeploy or a save in the Environment tab restarts the whole container, which also picks up `.env` — but that's a much bigger hammer and still subject to the `.env`-wins precedence above.

## Platform access and pairing

Separate from env vars: each chat platform has its own on-disk stores that gate who can talk to the agent. These live only on the instance's persistent disk, have no local mirror in this repo, and — unlike env vars — are **re-read from disk on every message**, so changes take effect immediately with no restart. Inspect them over SSH (see @SERVICES.md).

### LINE

Four mechanisms, three of them file-backed:

| Mechanism | Where | Reload |
| --- | --- | --- |
| `LINE_ALLOWED_USERS`, `LINE_DM_POLICY`, `LINE_ALLOWED_GROUPS`, `LINE_REQUIRE_MENTION`, … | `/opt/data/.env` | env var — **needs a verified restart** |
| Pairing approvals | `/opt/data/platforms/pairing/line-approved.json` (+ `line-pending.json`) | every message |
| One-off QR join invites | `/opt/data/platforms/line-invites/invites.json` | every message |
| Per-group response mode | `/opt/data/platforms/line-modes/modes.json` | every message |

**DM access is the union of env + pairing store.** A user is authorized if they're in **either** `LINE_ALLOWED_USERS` **or** `line-approved.json` — an OR, not one superseding the other. Someone can have working access purely from a redeemed invite code or an operator `hermes pairing approve`, and never appear in `.env` at all. So "who currently has LINE access" always requires reading both, over SSH. Render's dashboard/`.env` view shows only the env half and is never the full picture. (`clients/<slug>.env` is a fine local shortcut for the env half *if* it's been kept in sync — every change pushed, no manual edits since; the pairing half always needs SSH.)

**Pairing and invites** both flow through `PairingStore.is_approved()`. Under `dm_policy: pairing` (the default, from `patches/line-dm-pairing.patch`) an unrecognized DM falls through to the gateway's pairing logic and the sender gets a code for an operator to approve, rather than being dropped. `LineInviteStore` is deliberately a separate store: an invite token is minted ahead of time by the `line-invite` skill and grants access on redemption with no approval step. Because these re-read on every message, they're the right tool for a quick revoke/re-grant test loop.

**Group response mode** (`modes.json`, written by `GroupModeStore` in `modules/line/render_mention.py`) sits on the no-restart side too: `get_mode()` re-reads on every group message so the in-chat toggle (the Template Buttons bubble, or `mode always` / `mode mention` addressed to the bot) applies on the very next message. `LINE_REQUIRE_MENTION` / `LINE_MENTION_FOLLOWUP_SECONDS` are env vars and follow the restart rules, but they only supply the default for a group the store has never seen — **a stored mode always wins**, so never reach for `env-sync push` to change one group's behavior. This is distinct from `LINE_ALLOWED_GROUPS`, which is an env var and controls whether the bot responds in that group *at all*.

Treat all these JSON files as read-only unless the task calls for a change. If you must hand-edit one, back it up first, edit, confirm the new behavior, then delete the backup.

### Removing a LINE user's access

Check both DM stores first to know which one(s) actually grant access, then remove from each that does — removing from only one leaves them in via the other.

- **In `line-approved.json`:** `hermes pairing revoke <user_id>`. Effective on their very next message, no restart.
- **In `LINE_ALLOWED_USERS`:** remove their ID from `clients/<slug>.env`, run `hermes-env-sync push <slug>`, confirm it reports restart-verified, then confirm an `Unauthorized user: <id>` line for them in `logs/gateway.log` on their next message. The edit alone does nothing until a real, verified restart happens.

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
