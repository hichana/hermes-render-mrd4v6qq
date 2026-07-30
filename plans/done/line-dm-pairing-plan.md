# LINE DM Pairing & Onboarding — Implementation Plan

## Session handoff — read this first

This plan is written to be picked up cold, in a brand-new session with no memory of how it was produced. Everything you need is either in this file or discoverable from the repo/upstream source using the commands below — you should not need to ask the user to re-explain context that's already written down here.

**Where things actually stand right now:**

- **Done, verified, not yet deployed:** Phases 0–4 (a working `dm_policy`
  fix for LINE DM pairing). The code patch, tests, and a local Docker
  build + smoke test all passed. Nothing has been pushed to git or
  deployed to Render yet.
- **Done, verified, not yet deployed:** Phase 6c (the manager-initiated
  one-off QR invite feature). Built, unit-tested (103 passed, including
  17 new Phase 6c tests), and verified against a real local Docker build
  + smoke test — see the Phase 6c checklist below. Only Phase 6c-F (one
  real end-to-end test on an actual LINE channel) remains, since that
  needs a live channel and a second throwaway LINE account.
- **Superseded, do not build:** Phase 6b (real-time approve/deny
  notifications). Kept in this file for the record of why it was rejected
  in favor of Phase 6c — don't resurrect it without re-reading why.

**Repo state:** `git status` shows `patches/`, `scripts/bootstrap.sh`,
`scripts/patch-config.py`, `skills/` (all new/untracked) and a modified
`Dockerfile` — all of this is this project's work (Phase 2 + Phase 6c
combined) and is safe to commit together once the user approves a
deploy. `CLAUDE.md`, `README.md` (modified) and `whiteboards/`
(untracked) are unrelated pre-existing changes from outside this plan —
**do not touch, commit, or attribute them to this work.**

**If you need to read/edit actual Hermes source again** (you will, for Phase 6c): the exploration in this plan was done against a throwaway clone in a session-scoped scratch directory that will **not** exist in a new session. Re-clone it:

```bash
git clone --branch v2026.7.7.2 --depth 1 \
  https://github.com/NousResearch/hermes-agent.git /tmp/hermes-agent-src
```

Before trusting anything you read there as "what's actually deployed," re-verify it's byte-identical to the pulled image first (see the Phase 4 findings below for why this matters and the exact commands) — a public tag and a pulled image can in principle drift, so this check is cheap insurance, not ceremony.

**On every future session touching this plan:** check whether `HERMES_IMAGE` in the `Dockerfile` still matches what this plan was verified against (`v2026.7.20`). If it's been bumped, treat every patch in `patches/` as unverified until you re-run the regeneration procedure in `patches/README.md`.

> Patch-verification history. The plan's own body below was written against `v2026.7.7.2`; the line numbers and clone commands in it still say so deliberately, since that's the tree the patches were authored from.
> - `v2026.7.7.2` — original authoring and verification.
> - `v2026.7.20` (2026-07-30) — both patches re-verified to apply unchanged via UPGRADING.md Phase 2's in-image `git apply` check. Upstream's only change to `adapter.py` was a 2-line `hmac.compare_digest` bytes fix at ~L275, nowhere near any of our hunks, so no regeneration was needed.

---

Goal: close the gap where LINE is the one Hermes adapter that can't use DM pairing, so onboarding a new colleague on LINE stops requiring you to grep `gateway.log` for their `U…` ID and hand-edit `LINE_ALLOWED_USERS` in Render — and, further, replace even the manual "someone runs `hermes pairing approve`" step with a one-off QR code a manager hands a candidate right after an interview.

Confirmed by reading the actual source (`NousResearch/hermes-agent`, pinned in this repo's `Dockerfile` at `HERMES_IMAGE=docker.io/nousresearch/hermes-agent:v2026.7.7.2`): the DM-pairing gap is real and fixable in the LINE adapter specifically — not a LINE platform limitation, and not a fundamental Hermes limitation either. Four other "own-access-policy" adapters (WeCom, Weixin, QQBot, WhatsApp) already solve exactly this problem with a `dm_policy` switch. LINE just never got it wired up.

## Live-state check (Render, 2026-07-21)

Confirmed via the Render MCP server against the actual `hermes` service (`srv-d97k2t57vvec73ccpg2g`, workspace "All In GK's workspace"):

- The LINE webhook is live and taking real traffic today: `POST
  /line/webhook` → `200`, `userAgent="LineBotWebhook/2.0"`. So whatever
  patch ships here has to be validated against a channel that's already
  in production use, not a cold/unconfigured one.
- The running deploy (`dep-d9fe9srh523c73f24dcg`, commit `7cdc077`) is
  consistent with this repo's pinned `HERMES_IMAGE=v2026.7.7.2` — the
  version the source-level findings below were confirmed against.
- One `WARNING gateway.run: ✗ line failed to connect` appeared at
  `03:20:26`, exactly at that deploy's `finishedAt` timestamp — a
  boot-sequence transient (LINE adapter connecting before some
  dependency was ready), not an ongoing failure; webhook traffic
  succeeded normally afterward. Worth one line in the smoke test (Phase
  8) so a *persistent* version of this warning doesn't get waved off as
  "the same harmless boot blip" the next time someone reads the logs.
- Render's own env-var API is write-only from the MCP side (no "get"
  tool exists, by design — it merges into existing vars without pulling
  secrets into context), so the current live value of
  `LINE_ALLOWED_USERS` wasn't read here. Not needed for this plan; note
  it if a future task specifically requires knowing who's currently
  allowlisted.
- An unrelated dashboard-login bug and an `autoDeploy` config
  discrepancy were also found during this investigation — split out to
  `plans/dashboard-auth-login-bug-plan.md` since neither is a
  LINE/pairing issue.
- The user has Render's auto-deploy-on-push-to-main enabled, so once
  `patches/` + the `Dockerfile` change are committed and pushed, Render
  rebuilds and redeploys automatically — no manual "trigger deploy" step
  needed beyond the push itself. Still confirm with the user before that
  push, since it's a production action on a live, traffic-serving service.

## Reference — confirmed Hermes internals (read before touching source)

These facts were verified directly against the pinned tag's source and, where noted, against the actual pulled Docker image or LINE's own docs — not assumed. Re-derive/re-verify if `HERMES_IMAGE` has moved since.

- **`gateway/authz_mixin.py`, `_is_user_authorized()`** (~line 264) checks, in order: (1) per-platform allow-all, (2) env-var allowlists, (3) **DM pairing approved list**, (4) global allow-all, (5) deny. Item 3 means anything that lands a user in `PairingStore`'s approved list is sufficient for full authorization — no allowlist env var edit needed.
- **`gateway/pairing.py`, `PairingStore`:**
  - `generate_code(platform, user_id, user_name)` mints a code **bound to the `user_id` passed in at generation time** — hashed+salted, stored in a per-platform `pending.json`. `ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"` (no `0/1/I/O`), `CODE_LENGTH = 8`.
  - `approve_code(platform, code)` finds the pending entry whose hash matches, and approves **`matched_entry["user_id"]`** — i.e. whoever the code was generated *for*, not whoever presents it. **This is why you cannot pre-mint a Hermes pairing code for someone whose real LINE user ID isn't known yet** (Phase 6c's whole premise) — the approval would bind to a placeholder ID, not the real candidate.
  - `_approve_user(platform, user_id, user_name)` (leading underscore, but a plain callable method — must be called **under `self._lock`**) writes straight to the approved list and mirrors into the env-var allowlist via `_sync_allowlist_add`. This is the method Phase 6c uses directly: once the *real* LINE user ID is known (the candidate's first inbound message), call this to grant access immediately, bypassing the code-based flow entirely.
  - `MAX_PENDING_PER_PLATFORM = 3`, codes expire after 1 hour, 1 request/user/10min rate limit — all specific to the `generate_code`/`approve_code` path, irrelevant to Phase 6c's own invite store.
- **Hook system (`hermes_cli/plugins.py`, `VALID_HOOKS`):** a real, generic plugin-hook registry exists (`pre_gateway_dispatch`, `on_session_finalize`, etc.), but **there is no hook fired at pairing-code-generation time** in the current tag. Adding one would mean patching `gateway/run.py` (where `generate_code()` is called, ~line 8824) and `hermes_cli/plugins.py` — both shared, actively-changing core files. **Phase 6c avoids needing this entirely** (see below) — noted here so nobody rediscovers the same dead end.
- **Plugin/skill discovery:**
  - Bundled plugins load from `<repo>/plugins/<name>/` inside the image; a second location, `$HERMES_HOME/plugins/` (i.e. the mounted `/opt/data/plugins/` at runtime on Render), is also scanned — this repo's own `CLAUDE.md` "Pattern 3" already documents dropping capability in via an external dir at boot without forking upstream.
  - `kind: standalone` plugins (e.g. `plugins/google_meet`) register hooks via `register(ctx): ctx.register_hook("some_hook", callback)`. Not needed for Phase 6c (no new hook required), but relevant if a future phase does need one.
  - Any plugin — bundled or dropped-in — must additionally be present in `plugins.enabled` in `~/.hermes/config.yaml` (an opt-in allow-list); not automatic just by being discovered.
- **`qrcode==7.4.2`** is already a pinned dependency in `hermes-agent`'s `pyproject.toml` (under the `messaging` extra) — Hermes' own onboarding flow already renders QR codes with it. Phase 6c reuses this; no new dependency to add.
- **LINE's `oaMessage` URL scheme** (confirmed against LINE's own docs, not memory — [developers.line.biz/.../using-line-url-scheme](https://developers.line.biz/en/docs/messaging-api/using-line-url-scheme/)): `https://line.me/R/oaMessage/{percent-encoded LINE ID}/?{percent-encoded text}` opens a chat with your official account and **pre-fills** `{text}` into the message input box. Confirmed, in the docs:
  - The LINE ID can be a Basic ID or Premium ID; both it and the message must be percent-encoded UTF-8 (e.g. `@` → `%40`).
  - It **pre-fills, does not auto-send** — the user still has to tap Send. One extra tap, not a fully zero-touch flow.
  - **Not confirmed by the docs, and not yet tested live:** whether a user who isn't already a friend of the account gets prompted to add the account as a friend first, before the pre-filled message can be sent. This is standard LINE behavior for official-account deep links in general, but treat it as unverified until Phase 6c's build includes one real live test on the actual channel.

## Phase 0 — Root cause (confirmed in source)

- [x] `gateway/pairing.py`'s `PairingStore` is fully generic — it takes a `platform` string, not a hardcoded enum, and already falls back to a plugin's `platform_registry` entry for its allowlist env var. LINE's plugin already registers `allowed_users_env="LINE_ALLOWED_USERS"` (`plugins/platforms/line/adapter.py`, `register()`). **No changes needed here.**
- [x] `hermes pairing approve <platform> <code>` (`hermes_cli/subcommands/pairing.py`) takes `platform` as a free-text CLI arg, not a fixed choice list. `hermes pairing approve line ABCD1234` already works today. **No changes needed here.**
- [x] The actual gap is `plugins/platforms/line/adapter.py`, `_dispatch_event()` (~line 902–931). It runs its own three-list allowlist check (`_allowed_for_source`, ~line 419) and on failure does `logger.info("LINE: rejecting unauthorized source %s", source); return` — **the event is dropped before it ever reaches `self.handle_message()`**, so it never reaches the gateway's central authorization/pairing logic in `gateway/run.py` (~line 9260–9300), which is where `PairingStore.generate_code()` gets called and the auto-reply with a pairing code gets sent. Every other pairing-capable adapter forwards unauthenticated-but-pairing-eligible DMs through to that central logic; LINE never does.
- [x] The adapter also never overrides `enforces_own_access_policy` (default `False` in `gateway/platforms/base.py`) or exposes a `_dm_policy` attribute, which is how WeCom/Weixin/QQBot/WhatsApp signal "I gate access myself, but I still forward pairing-eligible DMs." LINE is stuck in a third, unintended state: gates access itself, but *doesn't* forward anything — so pairing is structurally unreachable no matter what CLI commands exist.
- [x] Reference implementation to mirror: `gateway/platforms/whatsapp_common.py` (`_is_dm_allowed` vs. `_is_dm_intake_allowed`, ~line 210–233) and `gateway/platforms/qqbot/adapter.py` (`_dm_policy`, ~line 211, `enforces_own_access_policy`, ~line 273). Both expose `dm_policy: open | allowlist | disabled | pairing`, default to `"pairing"`, and let unauthenticated DMs (not group/room messages — pairing is DM-only everywhere in Hermes) fall through to the gateway when the policy is `"pairing"` or `"open"`.

## Phase 1 — Interim stopgap (use today, no code changes)

Do this for the colleague you're onboarding *right now*, while Phase 2–4 ship:


- [x] Colleague scans the QR from the Messaging API tab, adds the bot as a friend, sends any message.
- [x] `grep "rejecting unauthorized source" /opt/data/logs/gateway.log | tail -5` to pull their `U…` ID.
- [x] Append it to `LINE_ALLOWED_USERS` in the Render Environment tab, comma-separated, save (this restarts the service).
- [x] Note this doesn't scale past a handful of people — that's the whole point of Phase 2.

## Phase 2 — Patch the LINE adapter

All changes in `plugins/platforms/line/adapter.py`. **Implemented** — see `patches/line-dm-pairing.patch` for the exact diff, generated from a clean clone of the pinned `v2026.7.7.2` tag (verified byte-identical to what ships in the actual pulled Docker image before editing).

- [x] In `LineAdapter.__init__`, alongside the existing three-allowlist block (~line 680), add a `_dm_policy` attribute mirroring QQBot/Weixin's pattern:
  ```python
  self._dm_policy = str(
      extra.get("dm_policy") or os.getenv("LINE_DM_POLICY", "pairing")
  ).strip().lower()
  ```
  Default `"pairing"` — so anyone who upgrades gets working pairing out of the box, without touching their config. `LINE_ALLOWED_USERS` keeps working exactly as before; pairing and the static allowlist are both grants, checked as a union (this is how `_is_user_authorized` already treats allowlist + pairing store for every other platform).

- [x] Add the `enforces_own_access_policy` property override (mirrors `qqbot/adapter.py` ~line 273, `weixin.py` ~line 1502, `whatsapp_common.py` ~line 78):
  ```python
  @property
  def enforces_own_access_policy(self) -> bool:
      """LINE gates group/room access itself; DM access follows dm_policy."""
      return True
  ```

- [x] Add an intake-vs-strict split, mirroring `whatsapp_common.py`'s `_is_dm_allowed` / `_is_dm_intake_allowed`:
  ```python
  def _is_dm_intake_allowed(self, user_id: str) -> bool:
      """Whether a DM may reach the gateway (pairing handshake path)."""
      if self._dm_policy == "disabled":
          return False
      if self._dm_policy == "allowlist":
          return bool(user_id) and user_id in self.allowed_users
      if self._dm_policy == "pairing":
          return True
      if self._dm_policy == "open":
          return self.allow_all
      return False
  ```

- [x] Rewrite the gate in `_dispatch_event()` (~line 913–922) so it only hard-drops group/room traffic and DMs under a non-pairing policy; DMs eligible for pairing fall through to `_handle_message_event`:
  ```python
  src_type = (source or {}).get("type", "")
  if src_type == "user":
      uid = source.get("userId", "")
      allowed = self.allow_all or (uid in self.allowed_users)
      if not allowed and not self._is_dm_intake_allowed(uid):
          logger.info("LINE: rejecting unauthorized source %s", source)
          return
      # else: fall through — either already allowlisted, or pairing-eligible
      # and the gateway's own _is_user_authorized/_get_unauthorized_dm_behavior
      # will handle the pairing-code reply for the unauthorized case.
  else:
      if not _allowed_for_source(
          source, allow_all=self.allow_all, user_ids=self.allowed_users,
          group_ids=self.allowed_groups, room_ids=self.allowed_rooms,
      ):
          logger.info("LINE: rejecting unauthorized source %s", source)
          return
  ```
  Groups/rooms keep today's strict allowlist-only behavior — matches WhatsApp's `_is_group_allowed`, which also returns `False` for `dm_policy: pairing` in groups. Pairing is a DM-only concept everywhere in Hermes; don't extend it to groups here.

  **Phase 6c will add one more branch inside this same `if src_type == "user":` block** — see Phase 6c below — checking an unrecognized DM's text against the invite-token store *before* falling through to `_is_dm_intake_allowed`. Read Phase 6c in full before editing this method again.

- [x] Confirm `self._reply_tokens[chat_id]` still gets stashed in `_handle_message_event` before the (now-reached) gateway authorization check runs, so the auto-reply pairing code goes out via LINE's free Reply API rather than the metered Push API. **Confirmed unchanged** — the stash happens at the top of `_handle_message_event`, before `await self.handle_message(event_obj)` (where the gateway's central pairing/authorization runs), exactly as before this patch.

- [x] Update `plugins/platforms/line/plugin.yaml`: add a `LINE_DM_POLICY` entry to `optional_env` (`open | allowlist | disabled | pairing`, default `pairing`), matching how QQBot/Weixin document theirs.

- [x] Add a short note to the `platform_hint` string or adapter docstring that pairing is now supported, so the agent doesn't tell a user something inaccurate if asked. (No `platform_hint` string exists in this adapter — added the note to the `LineAdapter` class docstring instead, which is the closest equivalent here.)


## Phase 3 — Tests

- [x] `tests/gateway/test_line_plugin.py` — added `TestDmPolicy` and `TestDispatchEventDmPairing`, mirroring the WeCom (`test_wecom.py` `TestPolicyHelpers`) and QQBot (`test_qqbot.py`) pairing test patterns:
  - Unrecognized DM with default `dm_policy` reaches `_handle_message_event` (doesn't get silently dropped).
  - Unrecognized DM with `dm_policy: disabled` / `allowlist` still gets dropped at the adapter, unchanged from today.
  - Unrecognized group/room message is still dropped regardless of `dm_policy` (pairing is DM-only).
  - Already-allowlisted user (and already-allowlisted group) still gets through unchanged (regression check on the existing three-list behavior).
  - Plus lower-level `TestDmPolicy` coverage: default/explicit/env-sourced `_dm_policy`, `extra` taking precedence over env, `enforces_own_access_policy`, and `_is_dm_intake_allowed` for all four policy values.
  - Tracked separately as `patches/line-dm-pairing.tests.patch` (see Phase 4 findings for why this isn't applied by the Dockerfile).

- [x] Ran `pytest tests/gateway/test_line_plugin.py -q` — **92 passed** (75 pre-existing + 17 new). Ran `pytest tests/gateway/test_line_plugin.py tests/gateway/ -k authz` — **18 passed, 2 skipped**, no regressions in the shared gateway authz suite. (Environment: `uv sync --frozen --extra dev --extra messaging` in a clean clone of the `v2026.7.7.2` tag, to get `pytest`/`pytest-asyncio` and the `aiohttp` dep the LINE adapter needs.)

- [x] **Phase 6c's own test coverage landed** — `TestLineInviteStore` (6 tests: mint length/alphabet, valid redeem + single-use, unknown/expired token fails safe, ordinary chat text never counts as a token attempt, per-uid lockout after repeated failed attempts without affecting other uids) and `TestDispatchEventInviteRedemption` (5 tests: valid invite grants access and skips the pairing-code fallthrough, creator notification, invalid token falls through unchanged, a redeemed token can't be reused by a third party, invite redemption works even under `dm_policy: disabled`). Full suite: **103 passed** (92 from Phase 2/3 + 11 new). See Phase 6c-D below for detail.


## Phase 4 — Build and deploy the patched image on Render

Decision: **patch-file route** (not a fork). Rationale and full verification
below — confirmed against the real pulled image and the real Dockerfile
build, not assumed.

### Findings — how the patch-file approach actually works in this repo

- This repo's image is *only* `FROM ${HERMES_IMAGE}` (currently `docker.io/nousresearch/hermes-agent:v2026.7.7.2`) plus a chown fix and a Caddy layer. Hermes' actual Python source (`gateway/`, `plugins/`, etc.) lives entirely inside the pulled upstream image — this repo never vendored, cloned, or otherwise touched that source before this project.

- Pulled the pinned image (`docker pull ...v2026.7.7.2`) and confirmed directly inside it:
  - `plugins/platforms/line/adapter.py` (1652 lines) is a plain, uncompiled `.py` file at `/opt/hermes/plugins/platforms/line/adapter.py` — not bytecode-only, not a frozen bundle. Directly patchable.
  - The image is **not** a git checkout (no `.git` anywhere) — a patch can't be generated with `git format-patch` from inside the image itself; it has to be generated from an external clone at the matching tag (see below).
  - `git` **is** present (`/usr/bin/git`, 2.47.3). `patch` is **not**. So the Dockerfile step must use `git apply`, never `patch -p1`.
  - The build stage runs as root at the point the patch needs to apply (the Dockerfile already does `USER root` before its other `RUN` steps), and the target directory (`/opt/hermes/plugins/platforms/line`) is root-owned — no permission issues applying mid-build.

- Cloned the *public* `NousResearch/hermes-agent` repo at the exact pinned tag (`git clone --branch v2026.7.7.2 --depth 1 ...`) and diffed `adapter.py`/`plugin.yaml` byte-for-byte against what's actually inside the pulled image — **identical**. This means: (a) the tag is a trustworthy diff base, and (b) a normal `git clone` + edit + `git diff` workflow produces a patch usable against the real image, without ever needing to `docker cp` files out by hand for day-to-day patch maintenance.

- Generated the patch via plain `git diff` inside that clone (not manual `diff -u` with hand-built `a/`/`b/` prefixes) — this is what makes the patch apply at `-p1`, the standard strip level `git apply` expects from a repo-root-relative diff.

- **Validated twice, independently:** (1) applied the patch inside a fresh throwaway container via `git apply -p1 --check` / `git apply -p1`, both clean; (2) built the *actual* image via `docker build .` with the Dockerfile changes below — the `git apply` step ran and applied cleanly as part of a real build, then `./scripts/smoke-test.sh` passed end-to-end (container boots, gateway reaches `running`, Caddy routes, dashboard auth gate armed). Also loaded `LineAdapter` inside the *built* image via its own `/opt/hermes/.venv/bin/python3` and exercised the new behavior live: `_dm_policy` defaults to `"pairing"`, `enforces_own_access_policy` is `True`, and `_is_dm_intake_allowed("Uunknown")` correctly returns `True`/`False` depending on policy.

- **Test files don't ship in the image** — confirmed no `/opt/hermes/tests` directory exists in the pulled image. So the patch applied by the Dockerfile (`patches/line-dm-pairing.patch`) covers only `plugins/platforms/line/adapter.py` and `plugins/platforms/line/plugin.yaml`. The Phase 3 test additions live in a **separate** file (`patches/line-dm-pairing.tests.patch`) that is never applied by the Dockerfile — it exists only for local re-verification against a real `hermes-agent` clone and to hand to Nous alongside the Phase 5 PR.


### What's now in this repo

- `patches/line-dm-pairing.patch` — the Phase 2 diff (adapter + plugin manifest), applied at Docker build time.
- `patches/line-dm-pairing.tests.patch` — the Phase 3 test diff, tracked but not applied by the Dockerfile (see above).
- `patches/README.md` — **read this before ever bumping `HERMES_IMAGE`**. Explains why the patch-file approach works here at all (plain `.py` source + `git` present + no `patch` binary), and gives the exact regenerate-and-validate procedure for when a version bump makes the existing patch fail to apply. Short version: **a patch generated against one tag is not guaranteed to apply against another** — `git apply` fails loudly (never silently mis-applies) if upstream touched the same file, so treat every `HERMES_IMAGE` bump as "go re-run the patches/README.md regeneration steps," not as a normal dependency bump. **Phase 6c adds a second patched file set to this same directory** — update `patches/README.md` when that lands so it lists all patches, not just this one.
- `Dockerfile` — new step right after `FROM ${HERMES_IMAGE}`:
  ```dockerfile
  USER root
  COPY patches/line-dm-pairing.patch /tmp/line-dm-pairing.patch
  RUN cd /opt/hermes && git apply -p1 --verbose /tmp/line-dm-pairing.patch \
   && rm /tmp/line-dm-pairing.patch
  ```
  (The pre-existing `USER root` further down, for the `chown` step, was redundant once this was added and was removed — the image was already root from this point on.)


### Checklist

- [x] **Patch-file route chosen** over forking the upstream image — avoids owning/hosting a separate image build+push pipeline for what's meant to be a temporary stopgap (see Phase 5). Verified `git apply` works against the real pulled image with no fork needed.
- [x] Generated `patches/line-dm-pairing.patch` from a clean clone of the exact pinned tag, applying the Phase 2 diff (`_dm_policy`, `enforces_own_access_policy`, `_is_dm_intake_allowed`, the `_dispatch_event` gate rewrite, `plugin.yaml`'s `LINE_DM_POLICY` entry).
- [x] Wired the `git apply` step into the `Dockerfile` right after `FROM ${HERMES_IMAGE}`, before the existing chown/Caddy steps.
- [x] Built the image locally (`docker build -t hermes-render:smoke .`) — patch applied cleanly as part of the real build, not just in isolation.
- [x] Ran `./scripts/smoke-test.sh hermes-render:smoke` — passed clean (container up, gateway running, Caddy routing, auth gate armed).
- [x] Loaded the patched `LineAdapter` inside the built image via its own venv and confirmed `_dm_policy`/`enforces_own_access_policy`/`_is_dm_intake_allowed` behave as designed.
- [ ] **Redeploy the Render service** — not done yet; this is a production action on shared infrastructure and needs an explicit go-ahead before pushing. **Phase 6c is now also built and locally verified** (see its checklist below) — `patches/line-dm-pairing.patch` and `patches/line-dm-pairing.tests.patch` already contain both Phase 2 and Phase 6c's changes in one combined diff, and a real `docker build` + `./scripts/smoke-test.sh` passed against it, so both can ship in one deploy as originally intended. Still ask before proceeding — this is a production action on a live, traffic-serving service, and Phase 6c-F (one real end-to-end test on an actual LINE channel) hasn't run yet. Once approved: commit `patches/`, `Dockerfile`, `scripts/patch-config.py`, `scripts/bootstrap.sh`, `skills/line-invite/`, and this plan update; push; then either let Render's existing auto-deploy pick it up or trigger a manual deploy; confirm boot is clean (health check + `[dashboard]` logs, per the README's troubleshooting table) same as any other deploy.


## Phase 5 — Upstream contribution

- [ ] Open a PR against `NousResearch/hermes-agent` with the Phase 2 diff — this is a small, well-scoped change that mirrors an existing pattern (WeCom/Weixin/QQBot/WhatsApp) applied to a fifth adapter, which is exactly the kind of change maintainers tend to accept quickly.
- [ ] Reference the actual gap in the PR description: LINE is absent from the adapters listed as pairing-capable, and `_dispatch_event` drops unauthenticated DMs before the gateway ever sees them, unlike the other own-access-policy adapters.
- [ ] Once merged and released in a tagged version, bump `HERMES_IMAGE` in this repo's `Dockerfile` back to the upstream tag and drop the fork/patch — track this as a follow-up so the fork doesn't become permanent maintenance burden.
- [ ] **Scope this PR to Phase 2 only.** Phase 6c's invite-redemption logic is deliberately kept out of any upstream submission (see Phase 6c's rationale) — don't let the two get tangled into one PR.


## Phase 6 — Baseline manual onboarding (fallback path, still valid)

This is what Phase 2–4 alone unlock: works today once deployed, no further code needed. Kept as the always-available fallback even after Phase 6c ships (e.g. if someone messages the bot without having gone through an interview/invite first).


- [ ] New colleague scans the QR from the Messaging API tab, adds the bot as a friend, sends any message.
- [ ] Bot auto-replies: *"Hi~ I don't recognize you yet! Here's your pairing code: `ABCD1234`. Ask the bot owner to run: `hermes pairing approve line ABCD1234`"* — this is the existing generic reply in `gateway/run.py`, unchanged, now finally reachable for LINE.
- [ ] You run `hermes pairing approve line ABCD1234` from the Render shell — or confirmed via Context7 docs, the dashboard exposes this as a real HTTP surface too: `GET /api/pairing` (list pending/approved) and `POST /api/pairing/approve` (`{"platform": "line", "code": "ABCD1234"}`), backing the dashboard's own Pairing page. Either path works with no env var edit, no service restart, no log grepping.
- [ ] If you already have `LINE_ALLOWED_USERS` configured, note that an approved pairing grant gets mirrored into that allowlist automatically (`gateway/pairing.py`, `_sync_allowlist_add`) — but only if the allowlist is non-empty already. On an already-open gateway (`LINE_ALLOW_ALL_USERS` or no allowlist at all), it stays in the pairing store only, which is fine — the authorization check unions both.
- [ ] **Know the burst limit:** `MAX_PENDING_PER_PLATFORM = 3` in `gateway/pairing.py` — only 3 pending codes per platform at once. If you're onboarding a batch of new employees the same day, approve (or `hermes pairing clear-pending`) as you go rather than having everyone message the bot simultaneously, or the 4th+ person will get "Too many pairing requests right now~" instead of a code.
- [ ] Pairing codes expire after 1 hour and are rate-limited to 1 request per user per 10 minutes — if a colleague's code expires before you approve it, they just message the bot again for a fresh one.


## Phase 6b — Manager self-service approval — SUPERSEDED, do not build

**Status: rejected in favor of Phase 6c below. Kept here only so nobody re-proposes this exact design without knowing why it was dropped.**

This phase's premise: a manager gets notified in real time when a stranger DMs the bot, and replies approve/deny. It was fully designed (see the git history of this file, or the reasoning trail below) before the user clarified the actual use case: a manager/employee interviews a candidate first, and the access decision is made *before* the candidate ever messages the bot. That reframing makes the entire reactive notify/approve/deny loop unnecessary — nobody needs to be pinged and asked "should I let this stranger in?" because the vetting already happened.

Two structural problems this design had, beyond just being unnecessary now:

- It required either patching `gateway/run.py` + `hermes_cli/plugins.py` to add a new `on_pairing_code_generated` hook (broadening the patch surface to core, actively-changing files — see the Reference section above), or fragile text-scraping of a canned reply string inside `send()`. Either way, meaningfully more patch-maintenance burden than Phase 2's single-file change.
- The user separately flagged real doubt that Nous would accept a PR touching `run.py`, which further pushed against this design even before the interview-based reframing made it moot.

If a future business need genuinely requires real-time approve/deny (e.g. walk-in candidates with no prior interview), revisit this section's design rather than starting over — but confirm the use case actually needs it first.


## Phase 6c — Manager-initiated one-off QR invite (chosen design — BUILT, locally verified)

**Built and verified** against a real local Docker build + smoke test (Phase 6c-E) and a full unit-test pass (Phase 6c-D). Only Phase 6c-F (one real end-to-end test on an actual live LINE channel) remains — needs a second throwaway LINE account and hasn't run yet, so treat the design as verified-in-the-lab but not yet field-confirmed.

### The use case

A manager (an existing, already-authorized Hermes user) interviews a candidate for a part-time role. Immediately after deciding to hire, the manager generates a one-off QR code and hands it to the new hire (in person, printed, AirDropped — whatever's convenient). The candidate scans it, LINE opens a chat with the bot with an invite token pre-filled, they tap Send, and they're immediately authorized — no further back-and-forth, no one else has to approve anything, because the interview *was* the approval.

### Why this doesn't need anything Phase 6b needed

- **No new Hermes hook, no `run.py`/`pairing.py`/`hermes_cli/plugins.py` patch.** The redemption side is a small addition to `_dispatch_event()` in `plugins/platforms/line/adapter.py` — the exact file Phase 2 already patches. See the Reference section: `_approve_user` is directly callable (under `self._lock`) to grant access the moment the candidate's real LINE user ID is known, and `_is_user_authorized` already consults that approved list as part of its normal checks — so once granted, everything downstream (agent access, dashboard listing, `hermes pairing` CLI visibility) just works, unchanged.
- **No external API/service.** LINE only allows one webhook per channel, and it's already pointed at this Hermes instance — so the redemption logic has to live somewhere in Hermes' own processing of that one webhook stream regardless. It turns out that's just the adapter file we already own.
- **No `PairingStore.generate_code()`/`approve_code()` reuse** — verified in the Reference section that those bind to the identity known at *generation* time, which doesn't exist yet for an unmet candidate. This phase uses its own separate, purpose-built invite store instead.


### What needs building

**A. Generation side — a new Hermes skill, not an adapter change:**

- [x] New skill (this repo's `CLAUDE.md` Pattern 3 — drop-in, no fork): `skills/line-invite/SKILL.md` plus `skills/line-invite/scripts/generate_invite.py`, giving the agent a documented workflow — "run this script, send the resulting QR image" — usable only by whoever the agent is already talking to (which, by construction, is always someone already authorized — an unauthorized DM never reaches the agent loop at all). Registered into `skills.external_dirs` at boot via `scripts/patch-config.py` (Pattern 1) run from a new cont-init hook, `scripts/bootstrap.sh` installed as `/etc/cont-init.d/03-render-tools` (Pattern 2) — verified live in the built image (see Phase 6c-E).

- [x] The script, when invoked:
  1. Mints a random one-off token via `LineInviteStore.mint()` (`plugins/platforms/line/adapter.py`, patched in — Phase 6c-B). **Sized independently of `pairing.py`'s `CODE_LENGTH=8`:** those codes are protected by `PairingStore`'s own per-user rate-limiting and lockout, which this separate invite store does not get for free. Uses a 16-char token from the same 32-symbol unambiguous alphabet (`LINE_INVITE_TOKEN_LENGTH`/`LINE_INVITE_ALPHABET`), plus its own per-uid guessing protection: 5 failed *token-shaped* attempts locks that uid out for an hour (`LINE_INVITE_MAX_FAILED_ATTEMPTS`/`LINE_INVITE_LOCKOUT_SECONDS`) — scoped per-uid, not global, and only text matching the token's length/alphabet counts as an "attempt," so ordinary DM chatter from strangers (Phase 1/6's manual-onboarding path) never trips it. Verified in `TestLineInviteStore` and by a live `docker exec` redemption/reuse check against the built image.
  2. Persists it to `<HERMES_HOME>/platforms/line-invites/invites.json` (own invite store, hash+salt at rest, atomic temp-file + rename, chmod 0600 — mirrors `PairingStore`'s own pattern) with: label (e.g. "Jamie — PT cashier"), created_by, created_at, expires_at, and a `redeemed` flag. **Single-use, enforced and tested** — a redeemed token cannot be reused by a second presenter (`test_token_cannot_be_redeemed_twice_by_a_third_party`).
  3. Builds the LINE `oaMessage` link (Reference section's exact URL shape and percent-encoding) from a new `LINE_BASIC_ID` env var, documented in `plugin.yaml`'s `optional_env`.
  4. Renders that link as a QR PNG with `qrcode` and prints its path in a JSON result; the SKILL.md instructs the agent to send that image back to the manager in their own chat (the *skill* doesn't send it itself — skills are guidance text run via the agent's own generic tools, not a registered typed tool with network access of its own).

- [x] Invite expiry: defaults to 48h (`--hours`, `LINE_INVITE_TTL_HOURS_DEFAULT`), documented in the SKILL.md as the default, adjustable per invite.


**B. Redemption side — extends the Phase 2 patch (same file, same patch mechanism):**

- [x] In `_dispatch_event()`'s `if src_type == "user":` branch, added `_try_handle_invite_redemption(event, uid)`, called *before* falling through to `_is_dm_intake_allowed`. On a match it:
  - Calls `PairingStore()._approve_user("line", uid, resolved_display_name)` **under `store._lock`**, exactly mirroring `approve_code`'s internal pattern.
  - Marks the invite token redeemed (`LineInviteStore.try_redeem` does this atomically as part of the match itself, not a separate step).
  - Replies to the candidate with `self.invite_welcome_text` and returns `True`, which the caller uses to `return` — does **not** fall through to the generic pairing-code reply (`test_valid_invite_grants_access_and_does_not_reach_pairing_handler`).
  - Non-matches return `False` and fall through to existing behavior unchanged (`test_invalid_token_falls_through_to_pairing_handler_unchanged`).
  - Notifies the invite's creator ("✅ `<label>` just joined via your invite!") via `self.send(created_by, ...)`, best-effort — wrapped in try/except so a failed notify never blocks the candidate's own grant (`test_valid_invite_notifies_the_creator`).

- [x] Added `LINE_BASIC_ID` to `plugins/platforms/line/plugin.yaml`'s `optional_env`.

- [x] Regenerated `patches/line-dm-pairing.patch` (and `patches/line-dm-pairing.tests.patch`) from the same working clone that already had the Phase 2 patch applied, so the file now contains the Phase 2 + Phase 6c diff combined. Re-validated with `git apply -p1 --check` against both a fresh clone of the pinned tag and the actual pulled Docker image (`docker run ... git apply -p1 --check`) — both clean, per `patches/README.md`'s regeneration procedure.


**C. Display name resolution (needed for a useful invite-redeemed notification, and for Phase 6's existing raw-ID-only limitation):**

- [x] Added `get_profile(user_id)` to `_LineClient` (`plugins/platforms/line/adapter.py`), calling LINE's `GET /v2/bot/profile/{userId}`, wired into the redemption path so the approval/welcome messages use a real display name instead of the raw `U…` ID when available. Best-effort — returns `None` on any failure (bad status, network error), and `_try_handle_invite_redemption` falls back to the raw uid in that case rather than blocking redemption on it.


**D. Tests:**

- [x] `TestLineInviteStore` (`tests/gateway/test_line_plugin.py`): mint returns a token of the configured length/alphabet; valid redeem returns the entry and enforces single-use; unknown and expired tokens fail safe; ordinary non-token-shaped chat text never touches the store's failure counter; repeated token-shaped guesses lock out only the guessing uid, not a genuine candidate presenting the real token afterward.

- [x] `TestDispatchEventInviteRedemption`: valid live token → `_approve_user` called once (verified via `PairingStore().is_approved`), welcome sent, no fallthrough to the pairing handler; creator gets notified; invalid/non-token text falls through unchanged (regression check against Phase 2's existing behavior); a redeemed token can't be reused by a third party; invite redemption works independent of `dm_policy` (tested under `disabled`).

- [x] Ran `pytest tests/gateway/test_line_plugin.py -q` — **103 passed** (92 from Phase 2/3 + 11 new). Ran `pytest tests/gateway/test_line_plugin.py tests/gateway/ -k authz` — **18 passed, 2 skipped**, no regressions.


**E. Local build/verify loop (same discipline as Phase 4, don't skip):**

- [x] Re-cloned the pinned tag fresh into `/tmp/hermes-agent-src`, re-verified byte-identical to the pulled image (`docker cp` + `diff -q`, both `adapter.py` and `plugin.yaml` IDENTICAL), applied the existing Phase 2 patch as a baseline, made the Phase 6c edits on top, regenerated `patches/line-dm-pairing.patch` + `patches/line-dm-pairing.tests.patch` via `git diff`, validated both with `git apply -p1 --check` against a second fresh clone AND the real pulled image directly. Rebuilt the actual image (`docker build -t hermes-render:smoke .`) — patch applied cleanly as part of the real build. Ran `./scripts/smoke-test.sh hermes-render:smoke` — passed clean (boots, gateway running, Caddy routing, auth gate armed). Additionally, inside the *built* image: confirmed `skills.external_dirs` was patched into `config.yaml` at boot, the `line-invite` skill files are present under `/opt/render-tools/skills-local/`, `LineInviteStore.mint`/`try_redeem` behave as designed (mint → redeem once succeeds → redeem again with the same token correctly fails), and `generate_invite.py` produces a valid `oaMessage` URL + QR PNG end-to-end via the image's own `/opt/hermes/.venv/bin/python3`.


**F. Live-flow verification (do this before trusting the design further):**

- [x] One real end-to-end test on the actual LINE channel (2026-07-23): removed a colleague's user ID from the allowlist, redeployed, confirmed he got the "not a known user" reply, generated a real invite link and sent it to him — he was able to message the bot and start chatting again. Confirms the redemption path (`_try_handle_invite_redemption` → `_approve_user`) works end-to-end on the real channel, not just in tests/local Docker.
- [ ] Still unconfirmed: whether a non-friend gets a LINE add-friend prompt before the pre-filled `oaMessage` can send (today's test used an existing contact of the bot, so this specific edge wasn't exercised).

**F.1 — Finding: `LINE_BASIC_ID` must be set via the Hermes Dashboard, not Render's Environment tab (confirmed 2026-07-23):**

Symptom: after the above test, the agent kept asking the user to paste in `LINE_BASIC_ID` manually when generating an invite, even though it had been set as a Render container env var. Root cause, confirmed by reading the actual Hermes source (`.scratch-hermes-src`, cloned from the pinned tag — see "Session handoff" below for why that dir isn't checked in):


- `render.yaml`'s own header comment already documented the convention: *"All provider keys, tool keys, and chat platform tokens are set through the Hermes dashboard (API Keys tab), which writes them to `/opt/data/.env` on the disk."* `LINE_BASIC_ID` is exactly this kind of value and should have gone through that path from the start, not Render's Environment tab.
- The `optional_env` entry we added to `plugins/platforms/line/plugin.yaml` (Phase 6c step B) is picked up by `hermes_cli/config.py`'s `_inject_platform_plugin_env_vars()` at CLI import time, which merges it into `OPTIONAL_ENV_VARS` — this is what makes `LINE_BASIC_ID` appear as a configurable field in the Dashboard's key-value settings UI.
- Setting it there calls `save_env_value()` (`hermes_cli/web_server.py`), which persists to `HERMES_HOME/.env` (`/opt/data/.env`, the mounted disk) — a live edit, no redeploy needed.
- The `line-invite` skill's own readiness check (`tools/skills_tool.py::_is_env_var_persisted`) reads that same `/opt/data/.env` file *first* and only falls back to the raw process environment if the key isn't there — so a Render-Environment-tab-only value can be invisible to the skill-setup flow even though it's technically in `os.environ`.

**Resolved 2026-07-23, automated so this never has to be a manual dashboard step per client instance:** added `scripts/seed-env-from-render.py`, a new boot-time idempotent seeder (repo CLAUDE.md Pattern 1, applied to `.env` instead of `config.yaml`) wired into the existing `scripts/bootstrap.sh` cont-init hook. Set `LINE_BASIC_ID` as a plain Render **Environment**-tab var per instance (easy to script at provisioning time), and the hook copies it into `/opt/data/.env` on first boot — insert-only, never overwrites a value later set from the dashboard.

Hit one real s6-overlay v3 gotcha getting this to actually work, worth recording since it'll bite the next boot-time script too: **cont-init scripts do not inherit Render/docker `-e` env vars by default.** They're captured into `/run/s6/container_environment/` files at container start, but a plain `#!/bin/sh` cont-init script doesn't see them — confirmed by `docker exec -u hermes <seeder>` finding `LINE_BASIC_ID` fine while the exact same script invoked from the real cont-init hook (via `s6-setuidgid hermes`) saw an empty value. Fix: `bootstrap.sh`'s shebang is now `#!/command/with-contenv sh`, not `#!/bin/sh` — that's what actually exports the container environment into the hook's process env before it forks children. Verified end-to-end with `./scripts/smoke-test.sh` (now asserts `LINE_BASIC_ID=@smoketest` lands in `/opt/data/.env` after a real boot, not just that the script exists).


## Phase 7 — Security sanity check

- [ ] Confirm defaulting `_dm_policy` to `"pairing"` (rather than `"open"`) doesn't create an open relay: pairing still requires you to manually run `hermes pairing approve`, so an unapproved DM never reaches the agent — it only ever gets the canned "here's your code" reply. This matches WeCom/Weixin/QQBot's existing default and doesn't conflict with `SECURITY.md §2.6`'s fail-open concern, which is specifically about `dm_policy: open` skipping authorization entirely.
- [ ] Confirm group/room behavior is unchanged — this patch is DM-only, so `LINE_ALLOWED_GROUPS`/`LINE_ALLOWED_ROOMS` continue to be the sole gate for group/room traffic, same as today.
- [ ] Re-read this repo's own Phase 4/5 guardrails (`plans/hermes-plan.md`) before rolling this out to real colleagues — nothing here changes the terminal/execute_code exposure decision, this only affects who can reach the agent's chat interface at all.
- [x] **Phase 6c-specific:** the invite-token match check happens *before* any LLM/agent involvement — `_try_handle_invite_redemption` runs entirely inside `_dispatch_event`, ahead of `_handle_message_event`/`handle_message`, same deterministic-Python-gate property this plan has insisted on throughout. Invite tokens are genuinely single-use, confirmed by both a unit test (`test_token_cannot_be_redeemed_twice_by_a_third_party` — redeem once, then a second throwaway uid presenting the same token is rejected and does not get approved) and a live `docker exec` check against the built image (second `try_redeem` call with the same token returned `None`). The per-uid guessing-protection lockout is confirmed to actually trigger under a rapid-fire wrong-token test (`test_repeated_failed_token_shaped_attempts_lock_out_that_uid_only` — 5 failed token-shaped attempts locks that uid out, a different uid and a real invite are unaffected), not just in code review.
- [ ] ~~Confirm the Phase 6b hook's manager check is the real gate...~~ — **N/A, Phase 6b superseded.** No longer applicable; left struck through rather than deleted so a reader understands this checklist item used to exist and why it's gone.


## Phase 8 — Verification

- [ ] Test with a throwaway LINE account (not your colleague) first: confirm an unrecognized DM gets the pairing-code reply, confirm `hermes pairing approve line <code>` grants access, confirm the throwaway account can then chat normally.
- [ ] Test the reject-unchanged paths: an unrecognized *group* message is still dropped silently, and (if you set `LINE_DM_POLICY=disabled` on a throwaway profile) an unrecognized DM is still dropped silently with no code sent.
- [ ] **Phase 6c end-to-end** (supersedes the old Phase 6b two-throwaway-account test that used to be listed here; this is Phase 6c-F, still pending): using one throwaway "manager" account (already authorized) and one throwaway "candidate" account, have the manager generate an invite via the new skill, scan the resulting QR with the candidate account, confirm immediate authorization with no approval step from anyone, and confirm the token can't be reused by a third throwaway account. The logic side of this (single-use enforcement, immediate grant, no-approval-step) is already covered by automated tests and a live in-container check — what's still unverified is the actual LINE client behavior: whether a non-friend gets an add-friend prompt before the pre-filled message can send, and whether tapping Send really does trigger the webhook with the token intact end-to-end on a real device.
- [ ] Only then have your actual colleague/manager go through the real flow from Phase 6/6c.
- [ ] Confirm `pytest tests/gateway/test_line_plugin.py` passes clean before merging the patch into whatever image Render is running.
- [ ] Re-check Render logs after deploy for a *persistent* (not one-off-at-boot) `✗ line failed to connect` warning — the live-state check above found exactly one transient instance tied to a deploy's finish time; a recurring version of it after this patch ships would mean the patch broke the adapter's connection setup, not just its authorization logic.

