## The boot-time seeder is gone

There used to be a `scripts/seed-env-from-render.py` cont-init step that copied a couple of Render Environment-tab vars into `.env` on boot. It was **insert-only** (never overwrote), so it worked exactly once per key and then went permanently, silently inert — edit the Render tab afterwards and nothing happened. It was removed in favor of `env-sync`. Don't reintroduce a boot-time seeder for this.

One live incident from it is still worth carrying forward: cont-init hooks under s6-overlay v3 do **not** see Render/docker `-e` env vars unless the shebang is `#!/command/with-contenv sh`. The seeder saw an empty value for its var purely because `bootstrap.sh` used plain `#!/bin/sh`.

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

