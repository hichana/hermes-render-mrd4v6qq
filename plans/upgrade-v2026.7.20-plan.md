# Hermes bump: `v2026.7.7.2` → `v2026.7.20`

Status as of **2026-07-30**: **deployed and verified live** (`79ba3ad`).
Followed `UPGRADING.md`. One verification step is left, and it needs a human in
a LINE group — see "Outstanding" below.

## Where it stands

| Phase | State |
|---|---|
| 0 — release notes | done |
| 1 — preflight | done: `1 blockers, 6 to review` |
| 2 — bump + build | done: patches apply unchanged, 99 unit tests, 9/9 smoke assertions |
| 3 — deploy | done — deployed by Matt |
| 4 — verify live instance | done except step 5's negative mention test |
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
5. **LINE round trip** — DM to an approved user replied; an @-mentioned group
   message replied. The negative half is still outstanding (below).
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

## Outstanding

- **Phase 4 step 5, negative half:** confirm a group message *without* an
  `@`-mention gets **no** reply in a mention-mode group. Must be attempted
  more than `LINE_MENTION_FOLLOWUP_SECONDS` (default 90s) after the last
  mention by that same user, or from a different user — otherwise the
  follow-up window legitimately answers an unmentioned message and the test
  proves nothing. This is the only assertion that distinguishes "the mention
  patch applied and imports" from "the mention patch gates."

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
