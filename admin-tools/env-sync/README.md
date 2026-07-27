# hermes-env-sync

Admin-only CLI that upserts per-client env vars into a deployed Hermes
instance's `/opt/data/.env` over SSH, then restarts the gateway and verifies
the restart actually happened. This is the sanctioned replacement for the
old Render-Environment-tab seeder (`scripts/seed-env-from-render.py`,
deleted) and for hand-editing `/opt/data/.env` over SSH.

Why not just set values in Render's Environment tab? Hermes loads
`$HERMES_HOME/.env` with `override=True` at gateway startup — `.env` always
wins over whatever Render injects, for any key it contains, even a blank
one. That made the old seeder (insert-only) a one-shot source: once a key
landed in `.env`, editing Render's tab again did nothing, silently. This
tool always wins on every run instead, for exactly the keys you tell it to
manage — never a silent no-op.

**Never packaged into the client image.** This tool, its `clients/`
directory, and its `.backups/` directory only ever run on an admin's
machine. See repo `PACKAGING.md`'s "Admin tooling (never packaged)" section.

## Day-to-day: edit a file, run one command

Once setup (below) is done once per client, this is the entire workflow
for changing anything — rotating a key, updating an allowlist, whatever:

1. Open `clients/<slug>.env` in any text editor and change the value.
2. Save it.
3. Run:
   ```sh
   uv run --project admin-tools/env-sync hermes-env-sync push <slug>
   ```
4. Read the diff it prints, type `y` to confirm.

That's it. It writes the change to the server, restarts Hermes, confirms
the restart actually happened, and tells you plainly whether it succeeded.
Nothing else needs to be right in that file except the one line you
changed — every other key you didn't touch is left exactly as-is.

## One-time setup (per client)

```sh
cd admin-tools/env-sync
uv sync
```

Then, from the repo root:

```sh
cp admin-tools/env-sync/registry.yaml.example clients/registry.yaml
# edit clients/registry.yaml: add your client's slug + SSH target

cp admin-tools/env-sync/client.env.example clients/<slug>.env
# edit clients/<slug>.env: fill in real values for that client
```

Both `clients/registry.yaml` and `clients/<slug>.env` are gitignored — they
hold real per-client secrets and must never be committed.

## Usage

```sh
# Read-only: show what would change, no writes, no restart.
uv run --project admin-tools/env-sync hermes-env-sync diff <slug>

# Preview a push without touching anything remote.
uv run --project admin-tools/env-sync hermes-env-sync push <slug> --dry-run

# Apply: upserts changed/added keys, prompts for confirmation, then
# restarts the gateway and verifies pid+start_time both changed.
uv run --project admin-tools/env-sync hermes-env-sync push <slug>

# Skip the confirmation prompt (still prints the diff first).
uv run --project admin-tools/env-sync hermes-env-sync push <slug> --yes

# Already know .env is correct and just need a restart+verify.
uv run --project admin-tools/env-sync hermes-env-sync restart-only <slug>

# Sanity-check the registry parses.
uv run --project admin-tools/env-sync hermes-env-sync list
```

## What `push` actually does

1. Fetches the remote `/opt/data/.env`.
2. Computes a targeted **upsert** against `clients/<slug>.env`: every key
   present in your local file replaces (or is appended as) the
   corresponding remote line, verbatim — quoting/spacing style and all.
   Every remote key **not** in your local file (e.g. something set by hand
   through Hermes' own dashboard) is left completely untouched, in its
   original position.
3. Prints the diff. Secret-shaped keys (`*_TOKEN`, `*_KEY`, `*_SECRET`,
   `*_PASSWORD`) are redacted to a length + short hash fingerprint; every
   other key (allowlists, IDs, URLs) is shown in full, since you need to
   actually read those to confirm they're right.
4. If there's nothing to change, stops here.
5. Otherwise prompts for confirmation (unless `--yes`), backs up the
   pre-write remote `.env` locally to `.backups/<slug>-<timestamp>.env`,
   then writes the new content atomically on the remote side (temp file +
   `mv`, same idiom as this repo's other boot-time patchers).
6. Sends `SIGUSR1` directly to the gateway's pid, as the `hermes` user
   (confirmed live: `hermes gateway restart` is a silent no-op on a
   container where the gateway is the bare main process rather than a
   service installed via `hermes gateway install` — and root cannot signal
   a `hermes`-owned process at all, since Render's runtime drops
   `CAP_KILL`; only the owning user can self-signal). Then polls
   `/opt/data/gateway.pid` until **both** `pid` and `start_time` differ
   from before. Only that — never an exit code or a "restart requested"
   log line — counts as proof the restart happened (see repo CLAUDE.md's
   "Env-var allowlist edits vs. pairing-store approvals" section for why).
   Times out after 200s (comfortably past the 180s default
   `agent.restart_drain_timeout`) and reports failure clearly if
   verification doesn't succeed.

## Testing

```sh
cd admin-tools/env-sync
uv run pytest
```

All tests are pure-function / fake-transport — no real SSH connection is
made. Only manual testing against a live instance exercises the actual
`ssh`/`scp` path.
