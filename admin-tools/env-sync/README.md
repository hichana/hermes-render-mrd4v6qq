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

**To remove a key entirely, don't just delete its line.** Deleting a
`KEY=value` line from the local file does *not* delete it remotely — that
would be ambiguous with "I never had an opinion on this key" (e.g. a value
set by hand through Hermes' own dashboard, which this file was never
tracking in the first place). Instead, write a bang marker in its place:

```
!SOME_KEY_YOU_WANT_GONE
```

(literally `!` followed by the key name, no `=`). `push` will show it as a
removal in the diff and actually delete that line from the remote `.env`.

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
   original position. A `!KEY_NAME` line locally removes that key's line
   from the remote file entirely (see "To remove a key entirely" above).
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
7. Appends one row to `push-log.csv` (see below) and reminds you to commit
   it.

## `diff` is one-directional — it cannot show you untracked remote keys

Worth internalizing before you trust a clean `diff`: the upsert is
deliberately one-way, so **`diff` only ever reports on keys present in
`clients/<slug>.env`.** A key that exists in the remote `/opt/data/.env` and
*not* in your local file is left untouched by design (step 2 above) and is
therefore invisible — `diff` prints `(no changes)` whether the remote has the
same 12 keys as you or those 12 plus 13 more.

That is the right *write* semantic (it's what stops this tool from clobbering
anything set through Hermes' own dashboard), but it means a clean `diff` proves
"nothing I track has drifted," never "the file matches." Those differ.

**Found in practice, 2026-07-30.** `clients/ngraph-main.env` had 12 keys and
`diff` was clean; the container had 25. Eleven of the extras were upstream tool
defaults Hermes writes itself (`BROWSERBASE_*`, `BROWSER_*`, `*_TOOLS_DEBUG`,
`TERMINAL_*`) and correctly none of our business. The other two were
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USERS` — a live bot credential and
its user allowlist, for a platform showing `connected`, existing **only** on the
Render volume. Untracked, unbacked-up, and unrecoverable had that disk been
lost. Both keys are listed in `client.env.example`, so this was an oversight
rather than a decision. They have since been recovered into the local file.

To audit the other direction, compare key *sets* (names are not secrets):

```sh
ssh <target> 'grep -oE "^[A-Za-z_][A-Za-z0-9_]*=" /opt/data/.env | tr -d "=" | sort' > /tmp/remote_keys
grep -oE "^!?[A-Za-z_][A-Za-z0-9_]*=" clients/<slug>.env | tr -d "=" | sort > /tmp/local_keys
comm -23 /tmp/remote_keys /tmp/local_keys   # in the container, untracked here
```

Then judge each result: business config or secrets belong in
`clients/<slug>.env`; Hermes' own tool defaults should stay remote-only. Worth
running after any bump, and any time you inherit an instance you didn't
provision.

## `push-log.csv` — the committed record of remote writes

A push changes a live client instance but otherwise leaves no trace in
git: it reads from `clients/<slug>.env` (gitignored — real secrets),
writes to a remote `/opt/data/.env` that's in no repo at all, and snapshots
to `.backups/` (gitignored for the same reason). That means repo history
has nothing to say about when a client instance last changed or what
changed on it — which is the first thing you want to know when something
starts misbehaving in production.

`push-log.csv` closes that gap. It is **committed on purpose**, one row per
remote-affecting run:

```
timestamp_utc,operator,slug,action,outcome,keys_added,keys_changed,keys_removed,gateway_pid,gateway_start_time
```

- **Key names only, never values.** `auditlog.build_record` reads names off
  the `Diff` and has no access to the right-hand side of any `KEY=value`
  line, so a secret can't reach this file by construction rather than by
  someone remembering not to log it. `tests/test_auditlog.py` asserts a
  known secret value never appears in a built row.
- **Failures are recorded too.** `outcome` is `ok`, `write-failed`, or
  `restart-unverified`. That middle state — remote `.env` written, restart
  *not* verified — is exactly the one worth a permanent record, since the
  instance is then running config that doesn't match what's on its disk.
- **Writing the row is never fatal.** By the time it runs, the remote write
  already happened; a local `OSError` prints a warning and nothing more.
- Commit it alongside whatever prompted the push. `git log -p --
  admin-tools/env-sync/push-log.csv` then reads as the deployment history
  for every client instance.

The first row is a backfill of the 2026-07-28 `LINE_ALLOWED_GROUPS` push
(reconstructed from the `.backups/` snapshot that immediately preceded it),
written by hand before this logging existed — `gateway_start_time` is blank
there because it wasn't recorded at the time.

## Testing

```sh
cd admin-tools/env-sync
uv run pytest
```

All tests are pure-function / fake-transport — no real SSH connection is
made. Only manual testing against a live instance exercises the actual
`ssh`/`scp` path.
