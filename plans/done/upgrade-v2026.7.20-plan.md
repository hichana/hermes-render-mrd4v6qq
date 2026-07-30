# Hermes bump: `v2026.7.7.2` → `v2026.7.20`

Status: **complete**. Deployed and fully verified live on **2026-07-30**
(`79ba3ad`), following `UPGRADING.md`. Nothing outstanding.

## Where it stands

| Phase | State |
|---|---|
| 0 — release notes | done |
| 1 — preflight | done: `1 blockers, 6 to review` |
| 2 — bump + build | done: patches apply unchanged, 99 unit tests, 9/9 smoke assertions |
| 3 — deploy | done — deployed by Matt |
| 4 — verify live instance | done, all six steps |
| 5 — close the loop | done |

Live instance confirms `version 0.19.0`, `release_date 2026.7.20`,
`config_version 33 == latest_config_version 33` (no pending config migration),
`gateway_state: running`, LINE + Telegram both `connected`.

### Phase 4 results

1. **Both supervised processes up** — `hermes gateway run --replace` and
   `caddy run`, not just the dashboard.
2. **`hermes doctor`** — 4 issues, none bump-related and all pre-existing: a
   missing `~/.local/bin/hermes` symlink, npm vulns in upstream's `web` and
   `ui-tui` workspaces, and unconfigured optional API keys. The known stale
   `mcp_servers.render` entry is still on the volume; `RENDER_MCP_API_KEY` is
   **not** set in `/opt/data/.env`, so it is inert cruft, not live Render
   access on a client-facing instance. Removal remains a deliberate manual
   edit (`scripts/patch-config.py` is insert-only).
3. **Changed default — acted on.** See below.
4. **Volume state survived.** `pairing/` (`line-approved.json`,
   `line-pending.json`, `_rate_limits.json`) and `line-invites/invites.json`
   intact. `line-modes/` does not exist, which is **correct** — no group has
   ever had an explicit mode set, so groups follow the `LINE_REQUIRE_MENTION`
   default, which the patch documents as mention-required. The store is
   created on first write.
5. **LINE round trip — fully verified.** DM from an approved user replied; an
   @-mentioned group message replied; an un-mentioned group message got **no**
   reply. That last one is the only assertion that distinguishes "the mention
   patch applied and imports" (which the smoke test proves) from "the mention
   patch gates" — so it's the one to insist on next bump.
6. **env-sync restart path proven** — `restart-only ngraph-main` reported
   `restart verified (pid=5214, start_time=988680740)`, i.e. both fields
   changed. `gateway/run.py`'s USR1 plumbing survived a 3359-line diff.

### `session_reset` — the one change with client impact (resolved)

`session_reset` was **absent** from `/opt/data/config.yaml`, so the instance was
inheriting the upstream default, which this release flipped `both` → `none`.
Client LINE sessions had silently stopped auto-resetting.

Fixed on 2026-07-30: an explicit block was appended to
`/opt/data/config.yaml` (backup at `config.yaml.bak-20260730-063125`) pinning
the pre-upgrade behavior, and confirmed effective via
`hermes config get session_reset.mode` → `both` after the restart.

```yaml
session_reset:
  mode: both
  idle_minutes: 1440
  at_hour: 4
```

Setting it explicitly also makes the instance immune to future flips of this
key — which is the general lesson: **a default we inherit is a setting upstream
controls.** When a `CONFIG DEFAULT` line in the preflight names a key we care
about, pin it rather than re-deciding it every bump.

## Follow-on work done the same day (2026-07-30)

Both prompted by findings above rather than by the bump itself.

**Render MCP cruft removed.** `plans/clean-boot-logs-plan.md` §3 is now
resolved — entry removed from the volume, patcher confirmed innocent, and
smoke-test assertion 8b added so a fresh boot can never ship it. The
transferable lesson is there, not here: config removed from the image is not
config removed from a running instance, because `/opt/data` survives deploys.

**`clients/ngraph-main.env` audited against the container.** `diff` was clean
but the file tracked 12 of the container's 25 keys, and two of the untracked
ones were `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USERS` — a live credential
and allowlist that existed only on the Render volume. Recovered into the local
file, verified byte-identical (`diff` still clean afterwards). The reason a
clean `diff` didn't catch it is structural and now documented in
`admin-tools/env-sync/README.md`: the upsert is one-way, so `diff` reports only
on keys you already track.

## Closed out

- Both `/opt/data/config.yaml` backups taken during the day's edits were
  diffed against the live file (showing only the two intended changes and
  nothing else), then deleted — the back-up → edit → confirm → delete
  discipline, completed rather than left half-done.
- `hermes-env-sync diff`/`push` now print the untracked remote keys that hid
  the Telegram drift, so that half of the invariant is enforced by the tool
  instead of by someone remembering to run `comm`. Informational only:
  `has_changes` deliberately excludes them, so push still writes nothing when
  untracked keys are the only finding. 46 env-sync tests pass; verified
  against the live instance, which lists exactly the 11 expected knobs.

## Two findings worth carrying forward

**The preflight could report a scary blocker it never measured.** The first run
said `MISSING(blocker): docker/main-wrapper.sh` — the ENTRYPOINT chain, the
`c113823` failure class. It was GitHub answering 429 to a burst of raw fetches;
`curl -sfL` collapsed that into "no file written," indistinguishable from a 404.
`curl -sI` on the same URL returned 200. Fixed: `fetch()` now reads
`%{http_code}` and returns fetched / genuinely-404 / couldn't-tell, and
couldn't-tell aborts with exit 3 printing **no** verdict line. Covered by
`tests/test_upgrade_preflight.py::TestTransientFetchFailure` over a real local
HTTP server, because `file://` cannot express a 429. Full writeup in
`HISTORICAL-GOTCHAS.md`.

**The config-default check has a structural blind spot.** It diffs
`cli-config.yaml.example` only, and that file now defers whole blocks to
`DEFAULT_CONFIG in hermes_cli/config.py`. The release notes flagged
`display.show_reasoning` as default-ON; it appears nowhere in the example file,
so the preflight could not have reported it either way. Checked by hand: it was
`True` at *both* tags, so the notes were describing an existing default rather
than a delta — no client impact this time. `hermes_cli/config.py` is now a
`review` entry in the manifest so the next bump at least says "go read it."
Parsing that Python dict properly is the real fix if this ever bites; until
then Phase 0's release-notes read is the only backstop, which is exactly why
that phase is not optional.
