# Hermes bump: `v2026.7.7.2` → `v2026.7.20`

Status as of **2026-07-30**: local work complete and verified. **Not yet deployed.**
Followed `UPGRADING.md`. This entry exists for the Phase 3-5 items that are not
done, and for the two findings that outlived the bump.

## Where it stands

| Phase | State |
|---|---|
| 0 — release notes | done |
| 1 — preflight | done: `1 blockers, 6 to review` |
| 2 — bump + build | done: patches apply unchanged, 99 unit tests, 9/9 smoke assertions |
| 3 — deploy | **not started** — needs a push + a manual Render deploy |
| 4 — verify live instance | **blocked on Phase 3** |
| 5 — close the loop | done except the Phase 4-dependent notes |

`Dockerfile`'s `ARG HERMES_IMAGE` is already on `v2026.7.20` on `main`'s working
tree. Nothing is deployed until someone triggers it — `render.yaml` sets
`autoDeployTrigger: off`.

## Outstanding — do these in order

1. **Deploy** (Phase 3). Push, then trigger from the Render dashboard.
   Remember `healthCheckPath: /api/status` is served by the dashboard and will
   report green over a wedged gateway. Render saying "live" is not evidence.
2. **Phase 4, all six steps.** Do not skip step 5 (the real LINE round trip) —
   Phase 2 proves the patches *apply and import*, never that they *work*.
3. **`session_reset.mode` decision — the one behavior change with client impact.**
   Upstream flipped the default `both` → `none`. Our `/opt/data/config.yaml`
   persists across deploys, so this only bites if the key is *absent* there and
   inherited. Check with `grep -nA3 session_reset /opt/data/config.yaml`:
   - present → nothing to do, we were never inheriting it.
   - absent → client LINE sessions silently stop auto-resetting after this
     deploy. Context grows until `/reset` or compression, which is a cost and
     context change nobody asked for. Set it explicitly to keep the old
     behavior; that's a deliberate edit, since `scripts/patch-config.py` is
     insert-only and will not do it.

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
