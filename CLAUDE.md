# CLAUDE.md — hermes-render

## What this repo is

A Docker template for deploying one [Hermes Agent](https://github.com/NousResearch/hermes-agent) instance per client business on Render. Each deployed instance is client-facing. **Only admins provision or manage Render resources** — never a deployed agent instance. As of the Render-tooling removal (see `plans/strip-render-tooling-plan.md` if it's still present locally; it's gitignored), this image carries no Render account access at all: no MCP server, no Render API key, no `render` CLI.

## Debugging a deployed instance over SSH (admin-only)

This is separate from the image itself (which, per above, has zero Render account access baked in) — it's Matt/admin tooling for inspecting a running instance from the outside, e.g. reading pairing/allowlist state that only exists on the instance's persistent volume, not in this repo.

- **Key**: `~/.ssh/render_hermes` (private) / `~/.ssh/render_hermes.pub` (comment `matt-render-hermes-debug`), added to the target Render service's SSH public keys in the dashboard.
- **Connect**: `ssh -i ~/.ssh/render_hermes <service-id>@ssh.oregon.render.com` — the "user" in the SSH target *is* the Render service ID (`srv-...`), not an account username. Find it on the service's page in the Render dashboard. Lands you in the container as `root`.
- Render's SSH gateway throws a `bad signature for ED25519 key` warning on the *host* key during connect (proxy behavior, not a MITM) — noisy but harmless; it still authenticates and connects fine.
- Once in, Hermes' persistent state lives under `/opt/data`, owned by the `hermes` user. Relevant to LINE pairing/allowlist debugging specifically:
  - `/opt/data/platforms/pairing/line-approved.json` — approved LINE user IDs + display names (this is the actual "allow list" for LINE DMs under `dm_policy: pairing`; see `patches/line-dm-pairing.patch`).
  - `/opt/data/platforms/pairing/line-pending.json` — outstanding pairing codes awaiting operator approval (may contain stale entries for since-approved users; harmless).
  - `/opt/data/platforms/line-invites/invites.json` — one-off QR invite tokens from the `line-invite` skill, redeemed or not.
- Treat this as read-only unless the task explicitly calls for a change — this is a live client-facing instance, not a dev box.

### Env-var allowlist edits vs. pairing-store approvals — different reload rules

Two mechanisms gate LINE (and other platform) DM access, and they do NOT reload the same way:

- **`{PLATFORM}_ALLOWED_USERS` / `_DM_POLICY` / `_ALLOW_ALL_USERS` env vars** are read once via `os.getenv()` at adapter construction (e.g. `LineAdapter.__init__` → `self.allowed_users`) and then cached for the process's lifetime. Editing `/opt/data/.env` does **nothing** to an already-running gateway — it requires an actual process restart to take effect.
- **Pairing-store approvals** (`hermes pairing approve/revoke`, or invite-token redemption via the `line-invite` skill) go through `PairingStore.is_approved()`, which re-reads its JSON file from disk on *every message* — no caching. These take effect immediately, never need a restart, and are the right tool for a quick revoke/re-grant test loop.

**Don't trust a "restart requested" log line as proof a restart happened.** The restart plumbing (`_spawn_gateway_restart_watcher` in `hermes_cli/gateway.py`) spawns a detached watcher that waits for the *old* PID to exit before spawning a new one — if the old process never actually exits, nothing changes and no error is raised. Verify instead: `cat /opt/data/gateway.pid` (compare `pid`/`start_time` before and after) or `ps -o lstart -p <pid>` — if the PID is unchanged, the restart didn't happen. For an env-var change specifically, confirm in `logs/gateway.log` that the sender you removed actually gets an `Unauthorized user: <id>` line on their next message — that's ground truth, not the restart-requested log line.

### Access is the union of both stores — `.env` alone won't show you who's in

A user is authorized if they're in **either** `{PLATFORM}_ALLOWED_USERS` (env) **or** the pairing store (`line-approved.json`) — it's an OR, not one superseding the other. Concretely: Render's dashboard Shell/`.env` view only shows the env-var side. Someone can have working chat access purely via a redeemed invite code or an operator `pairing approve`, and never appear in `.env` at all. So "who currently has LINE access" always requires checking both `/opt/data/.env` (`LINE_ALLOWED_USERS`) *and* `/opt/data/platforms/pairing/line-approved.json` over SSH — never trust the dashboard's env view alone as the full picture.

### Removing a user's access

Check both stores first (see above) to know which one(s) actually grant this user access, then remove from each accordingly:

- **If they're in `line-approved.json` (pairing store):** run `hermes pairing revoke <user_id>` (or edit the JSON directly, same backup-then-edit-then-confirm-then-delete-backup discipline as any other manual edit to this file). Takes effect immediately, on their very next message — no restart needed, since `is_approved()` reads the file fresh every time.
- **If they're in `LINE_ALLOWED_USERS` (env):** edit `/opt/data/.env` to remove their ID, **then actually restart the gateway and verify it** per the restart-verification steps above (`gateway.pid` PID/start_time change, then confirm an `Unauthorized user: <id>` line for them in `logs/gateway.log`). An edit alone does nothing until a real restart happens — this is the exact failure mode this section exists to guard against.
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
