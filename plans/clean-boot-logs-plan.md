# Clean Logs Plan

Goal: get `agnt-pulse-worker` (and other deployed instances) to boot and
run without spurious `WARNING`/`ERROR` log lines, so a real problem is
visible against a quiet baseline instead of buried in expected noise.
Triggered by a Render "server failure" email for `agnt-pulse-worker`
(2026-07-24) that turned out to be a routine restart — but the logs
around it were noisy enough that a real issue could easily hide in the
same pattern next time.

Each item below is a separate, independently fixable noise source found
in the 2026-07-24 logs. Fix order isn't fixed — pick whichever's easiest
to validate first (probably #4, since it's the only one with an
already-open investigation).

## 1. Capability probe warnings fire once per turn/interaction unconditionally

**Symptom:** every model turn logs a wall of `WARNING tools.registry:
check_fn <name> returned False; dependent tools will be unavailable this
turn` (browser dialog/CDP/computer-use/vision/kanban/terminal checks) and
`WARNING agent.auxiliary_client: Auxiliary Nous client unavailable: no
Nous authentication found` + `marking nous unhealthy for 60s
(payment / credit error)` — repeated 4x per turn in the sample.

**Why this happens:** this instance has no browser/vision/Nous
credentials configured (expected — it's a text-only LINE-facing agent),
so every capability check fails every turn, and every failure gets
logged at `WARNING`.

**Initial fix idea:** this is upstream Hermes behavior (`tools.registry`,
`agent.auxiliary_client`), not something in this repo's Dockerfile/patch
layer. Options, in order of effort:
- Cheapest: confirm whether Hermes has a config knob to disable unused
  capability checks entirely for a headless/text-only deployment (no
  browser, no vision, no Nous) rather than probing every turn.
- If no such knob exists, this may be worth a PR/issue upstream
  (`NousResearch/hermes-agent`) — the check failing is expected and
  shouldn't be `WARNING` level when the capability was never configured
  (vs. e.g. `DEBUG` or logged once at boot instead of every turn).
- If neither is viable short-term, a log-level filter at the container
  level (e.g. a logging config drop-in that downgrades these specific
  loggers) could suppress them without waiting on upstream — but this
  hides real auxiliary/Nous failures too, so treat as last resort.

## 2. `LINE_BASIC_ID` env-passthrough refusal on every relevant tool call

**Symptom:** repeated `WARNING tools.env_passthrough: env passthrough:
refusing to register Hermes provider credential 'LINE_BASIC_ID' (blocked
by _HERMES_PROVIDER_ENV_BLOCKLIST)`, followed by a `terminal` tool call
actually failing with `LINE_BASIC_ID is not set`.

**Why this happens:** `LINE_BASIC_ID` is a legitimate config value (not
a secret) that something in this image/skill is trying to read via the
sandboxed `execute_code`/terminal path, which is deliberately blocked
per `GHSA-rhgp-j443-p4rf` (credential-scrubbing security fix). This
matches the known issue already documented in the repo CLAUDE.md/commit
history (`add seed script when bootstrapping to fix env var issue for
line basic channel ID`) and `scripts/seed-env-from-render.py`.

**Initial fix idea:**
- Check whether `scripts/seed-env-from-render.py` is actually seeding
  `LINE_BASIC_ID` into `/opt/data/.env` on this instance — the boot log
  shows `[render-tools] /opt/data/.env: nothing to seed (already set or
  no Render env value)`, so confirm which of those two cases is true
  here. If "no Render env value," the Render service's env vars may be
  missing `LINE_BASIC_ID` entirely upstream.
- If it *is* set correctly in `.env`, the warning is about a *different*
  read path — something (a skill?) is calling `execute_code`/terminal to
  read it directly instead of using whatever internal channel Hermes
  provides for provider config. Find that call site and point it at the
  correct API instead of env passthrough.
- Confirm this is the LINE `line-invite` skill or similar by grepping
  `skills/` for `LINE_BASIC_ID` usage.

## 2b. Related: `read_file` denial for `/opt/data/.env`

**Symptom:** `WARNING agent.tool_executor: Tool read_file returned
error: Access denied: /opt/data/.env is a Hermes credential store and
cannot be read directly.`

**Why this happens:** same root cause as #2 — something (agent
self-improvement review, or a skill) is trying to inspect `.env`
directly instead of through a sanctioned config-reading tool/API.

**Initial fix idea:** same investigation as #2 — likely resolves
together once the actual caller is identified. Not urgent on its own
(the block is working as intended — defense in depth), but the warning
volume goes away once the underlying flow stops trying.

## 3. `render` MCP server unreachable — repeated reconnect/backoff spam

**Symptom:** `WARNING tools.mcp_tool: MCP server 'render' keepalive
failed` → 5 reconnect attempts with exponential backoff → `parking; will
self-probe every 300s until it recovers` (repeats every ~5min
thereafter). Also `mcp__render__list_services` failing with `no
workspace selected`.

**Why this happens:** per repo CLAUDE.md, this image was deliberately
stripped of all Render account access (no MCP server, no Render API key,
no `render` CLI) — deployed agent instances should never have Render
tooling. This `render` MCP entry appears to be stale config left over
from before the strip, still referenced in `~/.hermes/config.yaml` on
this *already-running* instance (config removal doesn't retroactively
edit a live instance's config — only fresh boots get the current
patcher output).

**Initial fix idea:**
- Check this instance's `/opt/data/config.yaml` (or wherever
  `mcp_servers`/`render` is registered) over SSH for a leftover `render`
  MCP entry. If found, this is old state from before the Render-tooling
  removal and should be removed (with the same backup-then-edit
  discipline as other manual `/opt/data` edits).
- Confirm the *current* boot patcher (`scripts/patch-config.py` /
  `03-render-tools` hook) no longer inserts a `render` MCP entry on new
  boots — it shouldn't, per the strip, so this should only affect
  instances provisioned before that change.
- Add this as a smoke-test assertion (Pattern 4 in CLAUDE.md): grep a
  fresh boot's `config.yaml` for absence of a `render` MCP entry, so a
  regression here is caught before shipping.

## 4. Dashboard `NotImplementedError` on `/auth/login` — already tracked

**Symptom:** `NotImplementedError: BasicAuthProvider is password-only;
there is no OAuth redirect flow. The login page POSTs to
/auth/password-login instead.` — full 500 traceback, seen twice
(08:31, 08:46) in this log window, in addition to the original
2026-07-21 occurrence.

**Status:** already has an open investigation —
see `plans/dashboard-auth-login-bug-plan.md`. That plan's next steps
(full traceback pull, reproduce via correct/incorrect login attempts,
confirm `HERMES_DASHBOARD_BASIC_AUTH_*` vars) apply directly here; this
entry just confirms the bug is still live and recurring, not a one-off.

**Initial fix idea:** see that plan. Worth prioritizing since it's a
real unhandled exception (not just a noisy-but-harmless warning like
the others above) and has recurred at least 3 times now.

## 5. Transient 502 on `/api/status` immediately after restart

**Symptom:** `ERROR http.log.error dial tcp 127.0.0.1:10001: connect:
connection refused` for Render's health-check probe, ~3 seconds after
Caddy started, ~2 seconds before `main-hermes`'s gateway actually became
ready (`HERMES_DASHBOARD_READY port=10001` logged 2s later).

**Why this happens:** s6 starts `dashboard`/`caddy`/`main-hermes` all at
"starting" simultaneously; Caddy comes up and starts accepting traffic
before the upstream gateway process has finished initializing, so any
health check landing in that ~2-4s window gets a 502. This is very
likely the literal event behind the "exited with status 1" / "server
failure" email — not a crash, just a race at boot.

**Initial fix idea:**
- Lowest effort: confirm with Render support/docs whether this 502 alone
  (with no restart loop) is what triggers the "server failure" email, or
  whether something else in the 06:59-07:30 gap is the real trigger.
- If it is the trigger: add a startup ordering/delay so Caddy doesn't
  route to the upstream until `main-hermes` is actually healthy — s6-rc
  supports service dependencies (`s6-rc-bundle`/`producer-for` files) or
  a readiness gate; check if the upstream Hermes image already exposes
  one before adding a fork-local patch.
  - Alternative if s6 dependency ordering isn't easily available:
    healthcheck config on Render's side (if adjustable) with a short
    grace period, or a lightweight liveness endpoint dashboard/caddy can
    check before allowing traffic through.
- Add a smoke-test assertion (Pattern 4) that checks for zero 502s
  during a fresh `docker run` boot cycle, to catch a regression here.

## 6. `line failed to connect` warning at boot

**Symptom:** one `WARNING gateway.run: ✗ line failed to connect` at
`03:20:26`, exactly at that deploy's `finishedAt` timestamp.

**Why this happens:** a boot-sequence transient — the LINE adapter
connecting before some dependency was ready — not an ongoing failure.
Webhook traffic succeeded normally afterward. Same family as #5: things
starting in parallel and one briefly losing the race.

**Initial fix idea:** the warning itself may be unavoidable without
upstream ordering changes, so the priority is making it *distinguishable*
rather than silencing it. Add one assertion to the boot smoke test
(Pattern 4 in ARCHITECTURE.md) that this warning appears at most once and
only within the boot window — so a *persistent* version doesn't get waved
off as "the same harmless boot blip" the next time someone reads the logs.
If #5's readiness-gating work lands, re-check whether this disappears
along with it.

## Suggested order

1. #4 (dashboard auth) — already has a plan, is a real bug, easy to pick
   back up.
2. #3 (stale render MCP entry) — likely a quick SSH check + config edit
   on the live instance, no code change needed.
3. #5 (502 race) — moderate effort, meaningfully improves "clean boot"
   signal, good smoke-test candidate.
4. #2/#2b (LINE_BASIC_ID passthrough) — needs finding the actual caller
   first; scope unclear until then.
5. #1 (capability probe spam) — likely upstream-owned; lowest priority
   unless a Hermes config knob turns out to already exist.

#6 isn't in this order — it's a one-line smoke-test assertion, so fold it
into whichever pass touches the smoke test (most likely #5's).
