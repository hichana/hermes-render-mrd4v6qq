# Replace the Render-env-seeder with an admin-run env-sync CLI

## Context

Per-instance business config (`LINE_BASIC_ID`, `LINE_PUBLIC_URL`,
`LINE_SLOW_RESPONSE_THRESHOLD`) used to be set as a Render Environment-tab
var and copied into `/opt/data/.env` on first boot by
`scripts/seed-env-from-render.py` (invoked from the `scripts/bootstrap.sh`
cont-init hook). That script was **insert-only**: once a key had a
non-empty value in `/opt/data/.env`, Render's Environment tab became
permanently inert for that key — Hermes' own
`hermes_cli/env_loader.py:load_hermes_dotenv()` loads `$HERMES_HOME/.env`
with `override=True`, so `.env` always wins over whatever Render injects,
but the seeder only ever wrote a key once. An admin editing Render's
Environment tab later (e.g. rotating a token), expecting it to take
effect, got a silent no-op — no error, no log line.

**Decision:** replaced with `admin-tools/env-sync`, an admin-run CLI tool
outside the image that SSHes into a client's instance, does a **targeted
upsert** of env vars from a local per-client file into the remote
`/opt/data/.env`, then triggers a gateway restart and verifies it actually
happened via `/opt/data/gateway.pid`'s `pid`/`start_time` pair (never
trusting an exit code or log line). `scripts/seed-env-from-render.py` and
its `bootstrap.sh` invocation are deleted. The manifest is **all**
per-client business config, not just the old seeder's 3 vars — including
access-control vars (`LINE_ALLOWED_USERS`) and real secrets
(`OPENROUTER_API_KEY`, `LINE_CHANNEL_ACCESS_TOKEN`, etc.) — replacing both
the seeder and the "SSH in and hand-edit `.env`" workflow previously
documented in CLAUDE.md.

## What shipped

**New tool — `admin-tools/env-sync/`** (uv project, sibling to `scripts/`
since it never gets packaged into the image):

- `src/hermes_env_sync/envfile.py` — pure-function upsert algorithm:
  local-file keys always replace (or get appended over) the matching
  remote line, verbatim raw text; every other remote key/comment/blank
  line is passed through byte-for-byte untouched.
- `src/hermes_env_sync/registry.py` — loads `clients/registry.yaml`
  (slug → SSH target/key) and resolves a client's local `.env` path.
- `src/hermes_env_sync/ssh.py` — shells out to system `ssh` (no
  paramiko/fabric dependency); atomic remote write via temp-file + `mv`.
- `src/hermes_env_sync/restart.py` — triggers `hermes gateway restart` and
  polls `gateway.pid` until **both** `pid` and `start_time` differ from
  the pre-restart baseline; treats a timeout as failure, never as a
  warning.
- `src/hermes_env_sync/diff.py` — renders the confirmation diff,
  redacting `*_TOKEN`/`*_KEY`/`*_SECRET`/`*_PASSWORD` values to a
  length+hash fingerprint, showing everything else in full.
- `src/hermes_env_sync/cli.py` — `diff` / `push [--dry-run] [--yes]` /
  `restart-only` / `list` subcommands.
- `tests/` — 19 unit tests (upsert edge cases, diff redaction, registry
  loading, restart pid/start_time verification via a fake transport), all
  passing (`uv run pytest`), no live SSH involved.
- `registry.yaml.example`, `client.env.example` — committed templates for
  onboarding a new client into the gitignored `clients/` directory.

**Deleted:** `scripts/seed-env-from-render.py`; the seeder block +
`ENV_SEEDER` var in `scripts/bootstrap.sh` (the file itself survives — it
still does the `$HERMES_HOME` chown/mkdir and runs `patch-config.py`).

**Edited:** `Dockerfile` (dropped the seeder `COPY`/`chmod`, rewrote the
explanatory comment), `PACKAGING.md` (dropped the seeder from the packaged
table/COPY block, added an "Admin tooling (never packaged)" section for
`admin-tools/` + `clients/`), `README.md` (Post-deploy setup section now
points at `env-sync` instead of the Environment-tab caveat), `CLAUDE.md`
(all three sections under "Env-var allowlist edits vs. pairing-store
approvals" — restart-mechanism attribution corrected to
`S6ServiceManager.restart()`/`s6-svc -t` rather than the non-s6-only
`_spawn_gateway_restart_watcher`; "Removing a user's access" now points at
`hermes-env-sync push`), `SERVICES.md` (new pointer section),
`scripts/smoke-test.sh` (removed the `LINE_BASIC_ID` seed assertion and its
container env var — that mechanism no longer exists to test), `.gitignore`
(`/clients/`, `admin-tools/env-sync/.backups/`).

**Not part of this change:** pruning Render's Environment tab back to just
`HERMES_*`/generated vars on the live instance — done directly by the repo
owner, not via a file change here.

## Verification status

- Unit tests: 19/19 passing (`cd admin-tools/env-sync && uv run pytest`).
- Static check: no dangling references to the deleted seeder outside of
  CLAUDE.md's intentionally-preserved historical incident narrative
  (Pattern 2) and the new README's "what this replaced" note.
- `./scripts/smoke-test.sh`: passing against the rebuilt image (seeder
  gone, `patch-config.py` still present, all other assertions unchanged).
- **Live end-to-end, completed 2026-07-27** against
  `srv-d97k2t57vvec73ccpg2g@ssh.oregon.render.com` on
  `LINE_SLOW_RESPONSE_THRESHOLD` (write → restart → verify → revert), with
  one real bug found and fixed along the way:

  **`hermes gateway restart` is a silent no-op on this deployment shape.**
  The gateway runs as the container's bare main process (started by
  `main-wrapper.sh`, never through `hermes gateway install`), so `hermes
  gateway status` reports "Running manually, not as a system service" —
  the CLI's restart subcommand has no registered service to dispatch to,
  exits 0, and changes nothing. The dashboard's own `POST
  /api/gateway/restart` shells out to the identical subcommand, so it's
  not a usable alternative either (confirmed by reading
  `hermes_cli/web_server.py`'s `_spawn_gateway_restart()`).

  **What actually works:** `kill -USR1 <pid>` sent directly, but *only* as
  the `hermes` user — root gets `Operation not permitted` (Render's
  runtime drops `CAP_KILL`; only same-UID self-signaling is exempt from
  that check). `restart.py` now sends this directly instead of shelling
  out to the CLI restart command; `ssh.py`'s `run_as_hermes()` also needed
  a fix separately (bare `s6-setuidgid` isn't on an interactive SSH
  session's `PATH` — needs the full `/command/s6-setuidgid`). Poll
  timeout raised from 30s to 200s to comfortably clear the 180s default
  `agent.restart_drain_timeout` (observed actual restart time on an idle
  instance: under 20s).

  Also checked whether Hermes' `/api/env` + `/api/gateway/restart` REST
  API (dashboard-auth-protected) would be a cleaner alternative to SSH —
  concluded no: the restart endpoint shares the identical bug, and the env
  write side (`save_env_value()`) only updates the dashboard process's own
  `.env`/`os.environ`, not the gateway process's already-cached adapter
  values, so a restart is unavoidable regardless of which write path is
  used. Stuck with SSH; not worth a second credential surface for no gain
  on the actual blocker.

  Both `CLAUDE.md`'s restart-verification section and
  `admin-tools/env-sync/README.md` updated with these corrected facts.
