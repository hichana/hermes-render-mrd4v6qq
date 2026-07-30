# Upgrading Hermes (bumping `HERMES_IMAGE`)

Run this every time. Not just when the release notes look scary.

**The one thing to internalize:** this repo pins an upstream image (`Dockerfile`, `ARG HERMES_IMAGE`) and layers patches, a `COPY`'d module, a Caddy service and a cont-init hook on top of it.
`/opt/hermes` is **ephemeral** — rebuilt from scratch on every deploy — so every one of those layers has to re-apply against the new tag, every time.
`/opt/data` (the Render disk) is the only thing that survives, which is also where every config migration lands.
Upstream ships ~2,000+ commits between releases. Assume every bump can move the ground under the image.

**Why the ceremony:** the one bump we've done (`eec0255`, 2026-07-09, `v2026.5.7` → `v2026.7.7.2`) was a bare one-line tag edit.
Upstream had moved to s6-overlay, repointed `/usr/bin/tini` at `/init`, and dropped `gosu`.
The container booted wedged and **stayed that way for 8 days** while `healthCheckPath: /api/status` kept answering `200`.
Six commits to clean up (`c113823` onward). Every phase below exists because of something in that incident.

---

## Phase 0 — Decide

1. Read the upstream release notes for the candidate tag: <https://github.com/NousResearch/hermes-agent/releases>.
   You are reading for three things only, in this order:
   - **changed defaults** (they hit the live instance's `/opt/data/config.yaml`)
   - **moved/renamed internals** (they hit our patches)
   - **changed container/boot mechanics** (they hit the whole deploy)

   Everything else — new providers, desktop app perf, TUI polish — is irrelevant to us and safe to skim.
2. Note the tag.
   Upstream tags look like `v2026.7.20`; the image is `docker.io/nousresearch/hermes-agent:<tag>`.

## Phase 1 — Preflight (~1 minute, no build, no Docker)

```bash
./scripts/upgrade-preflight.sh v2026.7.20        # candidate; current tag read from ./Dockerfile
```

It fetches every upstream file this repo reaches into, at both tags, and diffs them.
Read `scripts/upgrade-preflight.sh`'s `DEPS` / `SYMBOLS` / `STRUCTURE` tables — that manifest *is* the list of things a bump can break, and it's shorter than you'd fear.

The last line is `PREFLIGHT: <n> blockers, <m> to review`.
Non-zero exit means do not build yet.

### What each finding means

| Finding | What it means | Do this |
|---|---|---|
| `DRIFT(blocker): plugins/platforms/line/adapter.py` or `plugin.yaml` | Upstream touched a file we `git apply` a patch to. May still apply cleanly (context matching tolerates offsets); may not. | Run the cheap check in Phase 2 first. Only regenerate if it actually fails. |
| `MISSING(blocker): <path>` | Upstream deleted or moved a file we depend on. | Find where it went. This is a rework, not a regeneration. |
| `SYMBOL LOST(blocker): <name>` | A class/function/signal our patches or `admin-tools/env-sync` reference by name is gone. The file diff may look small — that's why this check exists separately. | Rework the patch (or, for `request_restart`/`SIGUSR1`/`gateway.pid`, `admin-tools/env-sync/src/hermes_env_sync/restart.py`) against the new upstream shape. |
| `STRUCTURE(blocker): ENTRYPOINT …` / `… s6-overlay …` / `… cont-init.d …` | **This is the `c113823` class of failure — the 8-day one.** Container boot mechanics changed. | Stop. Re-read the `Dockerfile`'s closing "Deliberately NO ENTRYPOINT override" comment block and `ARCHITECTURE.md`'s "Use of S6" before touching anything. Do not "fix" it by overriding `ENTRYPOINT`. |
| `DRIFT(review): <path>` | We only depend on named symbols inside it, and those all still exist. These files churn every release. | Skim the diff if the release notes flagged something in that area; otherwise proceed. |
| `CONFIG DEFAULT: …` | An upstream **default** changed. Does not affect the image at all. Affects a live instance whose `/opt/data/config.yaml` *inherits* that default instead of setting it explicitly. | Carry it into Phase 4. Don't act on it now. |

If a blocker turns out to be a genuine upstream move, add or adjust the manifest entry in `scripts/upgrade-preflight.sh` so the next bump knows about the new location — and add a test case to `tests/test_upgrade_preflight.py`.

**Worked example — `v2026.7.7.2` → `v2026.7.20` (verified 2026-07-30):**
1 blocker, 6 to review.
The blocker was `adapter.py`, a single 2-line `hmac.compare_digest` fix at ~L275, nowhere near any of our hunks — both patches apply unchanged.
`plugin.yaml`, `gateway/pairing.py` and `docker/main-wrapper.sh` byte-identical; all 15 tracked symbols and all 6 image-shape assumptions intact.
The real finding was in the config section: `session_reset.mode` default flipped `both` → `none`.
Also worth knowing: `/usr/bin/tini` is no longer a symlink to `/init`, it's a real shim script (`docker/tini-shim.sh`) — harmless for us because we don't override `ENTRYPOINT`, and a good reminder of why we don't.

## Phase 2 — Bump and build locally

1. Edit **one line**: `Dockerfile`'s `ARG HERMES_IMAGE=...:<new-tag>`.
   Nothing else, yet.

2. If Phase 1 flagged a patch-target blocker, check the patches *before* building.
   Our build is short — the layers on top of the base image take well under a minute (measured: ~33s, of which 20s is the `chown -R`) — but the *first* build on a new tag has to pull a fresh multi-GB base image, so in practice it's network-bound and worth not repeating on a guess.
   The check below is seconds and needs no build at all.

   **Order matters** — `line-group-mention.patch` is generated against a tree that already has `line-dm-pairing.patch` applied, so check them in sequence, not independently:

   ```bash
   TAG=v2026.7.20
   docker run --rm -v "$(pwd)/patches:/p:ro" --entrypoint sh \
     docker.io/nousresearch/hermes-agent:${TAG} -c \
     'cd /opt/hermes \
        && git apply -p1 /p/line-dm-pairing.patch \
        && git apply -p1 /p/line-group-mention.patch \
        && echo BOTH APPLY'
   ```

   Anything other than `BOTH APPLY` → regenerate, following `patches/README.md`'s "Regenerating a patch after bumping `HERMES_IMAGE`".
   Phase 1 has already done that recipe's steps 1-2 for you (it proved which files moved and by how much), so start at its step 3.

3. Unit tests — no Docker needed, so run them first:

   ```bash
   python3 -m pytest tests/ -q
   ```

4. The boot smoke test. **This is the gate.** It builds the image and boots it with Render's actual runtime restrictions (`--security-opt no-new-privileges`, `--cap-drop NET_BIND_SERVICE` — Render is stricter than stock Docker, and that difference has already caught one production-only failure):

   ```bash
   ./scripts/smoke-test.sh
   ```

   Nine assertions, each against a live artifact rather than a log line: the container stays up, `gateway_state=running`, Caddy owns `:10000`, the dashboard auth gate is armed, `/line/*` routes to the LINE backend and not the dashboard, `render_mention.py` is importable as part of the `plugins.platforms.line` package, the patched adapter imports, `skills.external_dirs` actually landed in `/opt/data/config.yaml`, and the `line-invite` skill's imports (`LineInviteStore`, `qrcode`) resolve.

   A `git apply` failure aborts the build loudly, which is the good case.
   The failure mode this test exists for is the quiet one: an image that builds fine and boots broken.

   Extend this file rather than adding a separate runner for anything new that only matters at boot.

## Phase 3 — Deploy

`render.yaml` sets `autoDeployTrigger: off`, so deploying is a deliberate act: push the commit, then trigger the deploy from the Render dashboard (or `render deploys create`).

Then, before you believe the green checkmark:

> `healthCheckPath: /api/status` is served by the **dashboard**, and is unauthenticated by design.
> A container whose gateway is wedged but whose dashboard still answers **passes this health check**.
> That is exactly how the last bump stayed broken for 8 days. Render saying "live" is not evidence.

Phase 4 is the actual verification.

## Phase 4 — Verify the live instance

SSH in per `SERVICES.md` (`ssh srv-…@ssh.oregon.render.com`).
**Read-only unless a step says otherwise** — this is a client-facing instance.

1. **Both supervised processes are really up** (not just the dashboard):

   ```bash
   ps -eo args | grep -E "gateway run|caddy run" | grep -v grep
   ```

   Two matches. One match, or none, means wedged — regardless of what
   `/api/status` says.

2. **Deprecated / unknown config keys on the volume.** `/opt/data/config.yaml` persists across deploys, so it accumulates keys that upstream has since renamed or dropped:

   ```bash
   hermes doctor
   ```

   Known cruft on our instance: a stale `mcp_servers.render` entry left from the Render-tooling strip (`plans/clean-boot-logs-plan.md` §3).
   Note that `scripts/patch-config.py` is insert-only by design and will never clean any of this up — removals are a deliberate manual edit.

3. **Changed defaults from Phase 1.** For each `CONFIG DEFAULT` line the preflight printed, confirm the key is written *explicitly* in `/opt/data/config.yaml` rather than inherited:

   ```bash
   grep -nA3 "session_reset" /opt/data/config.yaml   # e.g. for the v2026.7.20 flip
   ```

   Absent means the new upstream default now applies.
   For `session_reset` going `both` → `none`, that means client sessions silently stop auto-resetting — a cost and context change nobody asked for.
   Set it explicitly if you want the old behavior.

4. **Volume state survived.** The four LINE JSON stores (`SERVICES.md`), all re-read on every message, no restart needed:

   ```bash
   ls -l /opt/data/platforms/pairing/ /opt/data/platforms/line-invites/ \
         /opt/data/platforms/line-modes/
   ```

5. **A real LINE round trip.** Nothing above proves the patches *work*, only that they applied and import:
   - a DM from an already-approved user → gets a reply (pairing patch + the approved-users store)
   - a group message *without* an `@`-mention in a `mention`-mode group → no reply
   - the same group message *with* an `@`-mention → reply (mention gate + `line-modes/modes.json`)

6. **The restart path env-sync depends on.** Every future per-client env change goes through this, so prove it now rather than during an incident:

   ```bash
   uv run --project admin-tools/env-sync hermes-env-sync restart-only ngraph-main
   ```

   It sends `USR1` as the `hermes` user and only reports success once **both** `pid` and `start_time` in `/opt/data/gateway.pid` have changed.
   A `RestartVerificationError` means `gateway/run.py`'s restart plumbing moved — fix `admin-tools/env-sync/src/hermes_env_sync/restart.py` before you need it.
   (Note: `hermes gateway restart` and the dashboard's restart button are both silent no-ops on this deployment — see `ARCHITECTURE.md`.)

### Rollback

Revert the `Dockerfile` tag, redeploy.
Cheap and safe: the image carries no state, and `/opt/data` is untouched by a deploy.
If step 5 fails and the cause isn't obvious within a few minutes, roll back first and debug after — a client is on the other end of that LINE channel.

## Phase 5 — Close the loop

The bump isn't done when it's deployed; it's done when the next one is cheaper.

**Why this phase is not optional.** `scripts/upgrade-preflight.sh` is a **closed-world** check: it verifies the dependencies in its manifest and is blind to everything else.
Two things it structurally cannot catch, both of which only get fixed here:

- **New dependencies we add.** Every time this repo starts reaching into a new upstream path — a second patch, another `COPY` into `/opt/hermes`, an import of an upstream module from a skill script — that path has to be added to `DEPS`/`SYMBOLS` or the next bump silently stops checking it.
  The preflight passing means "nothing I know about moved," never "nothing moved."
  `tests/test_preflight_manifest_coverage.py` now enforces this for anything declared as a patch target, a `COPY` into `/opt/hermes`, or an upstream import: add one without registering it and `pytest` goes red (see `ARCHITECTURE.md`, "Keeping the preflight manifest honest").
  It cannot cover a dependency expressed only as a runtime path in a shell script — those still need a manual manifest entry.
- **New upstream mechanisms we haven't adopted yet.** If upstream introduces a new config layout, a new plugin discovery path, or a new supervision hook, the manifest has no entry for it because we weren't depending on it.
  That's what Phase 0's release-notes read is for — the manifest handles regression, the release notes handle novelty.
  Neither substitutes for the other.

- [ ] Update the version-specific comments the bump just invalidated.
      In the `Dockerfile`: the `chown -R hermes:hermes /opt/hermes/ui-tui` block's note about ink-bundle/prebuilt-`entry.js`, and the closing "Deliberately NO ENTRYPOINT override" block's tini story.
      Elsewhere: `ARCHITECTURE.md`'s tag references and `plans/done/line-dm-pairing-plan.md`'s "verified against" tag (it tells future sessions to treat every patch as unverified once the pin moves — so it has to be told the pin moved).
- [ ] Add anything newly learned to `HISTORICAL-GOTCHAS.md` — specifically the shape "X looked healthy but wasn't, here's what actually proved it."
- [ ] Extend `scripts/upgrade-preflight.sh`'s manifest with any dependency this bump revealed that it didn't already know about, plus a case in `tests/test_upgrade_preflight.py`.
- [ ] Bump the tag in `.claude/settings.local.json`'s allowlisted upstream `curl`, so the next session doesn't get a permission prompt mid-check.
- [ ] Note the release you're now on, and anything deliberately left undone, in a `plans/` entry if it needs follow-up.
