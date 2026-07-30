## The boot-time seeder is gone

There used to be a `scripts/seed-env-from-render.py` cont-init step that copied a couple of Render Environment-tab vars into `.env` on boot. It was **insert-only** (never overwrote), so it worked exactly once per key and then went permanently, silently inert — edit the Render tab afterwards and nothing happened. It was removed in favor of `env-sync`. Don't reintroduce a boot-time seeder for this.

One live incident from it is still worth carrying forward: cont-init hooks under s6-overlay v3 do **not** see Render/docker `-e` env vars unless the shebang is `#!/command/with-contenv sh`. The seeder saw an empty value for its var purely because `bootstrap.sh` used plain `#!/bin/sh`.

## A red preflight looked like upstream drift but was GitHub rate limiting

**Found during the `v2026.7.7.2` → `v2026.7.20` bump (2026-07-30).** The first `scripts/upgrade-preflight.sh` run reported `MISSING(blocker): docker/main-wrapper.sh — not present at v2026.7.20` — the ENTRYPOINT chain, i.e. the single scariest thing that check can say, and precisely the `c113823` failure class. It also inflated the churn counts on several `review` files (`gateway/run.py` at 3359 changed lines, the whole file).

None of it was real. `curl -sI` on that exact URL returned **HTTP 200**: upstream ships the file unchanged. GitHub had answered **429** to the burst of raw fetches, and the script used `curl -sfL`, which collapses every non-2xx into "no file written" — indistinguishable from a genuine 404. A rate-limited run therefore produced a *confidently wrong* verdict, and following it would have meant reworking patches against an upstream move that never happened.

What actually proved it: `curl -sI <same-url>` per suspicious path, checking the status code directly rather than re-running the script. A second run after the limit cleared came back `1 blockers, 6 to review`, matching UPGRADING.md's recorded worked example exactly.

Fixed in `scripts/upgrade-preflight.sh`: `fetch()` now reads `%{http_code}` and returns a tri-state — fetched / genuinely 404 / **couldn't tell** — and a "couldn't tell" aborts the whole run with exit 3 and prints no verdict at all, rather than reporting drift it never measured. `tests/test_upgrade_preflight.py::TestTransientFetchFailure` covers it over a real local HTTP server, since `file://` can't express a 429.

The transferable shape: **this file's usual lesson is "green didn't mean healthy." This is the mirror image — red didn't mean broken.** A checker that can't distinguish "the answer is no" from "I couldn't ask" will eventually tell you the scariest version of the story. When a preflight blocker and the recorded history disagree, verify the blocker against the artifact before believing it.

## Deleting a profile safely

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

