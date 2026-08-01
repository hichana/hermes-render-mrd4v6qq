# 07 — Can gateway multiplexing give each client agent its own LINE channel?

**Date:** 2026-08-01 (revised same day after a constraint clarification — see below)
**Question asked:** Hermes 0.19.0 added `gateway.multiplex_profiles` (one gateway process serving many profiles). Can we use it so one client's container runs N agents, each with its own LINE channel/identity, routed via the documented `/p/<profile>/` URL-prefix mechanism?
**Verdict: No, not as implemented.** Multiplexing's per-profile HTTP fan-out is real, but it was built for exactly two platforms (`webhook`, `api_server`) and LINE structurally cannot use it. At most one LINE channel can be served per gateway process, full stop, and this is enforced code, not a missing feature we just haven't configured yet.

> **Revised 2026-08-01.** The first pass of this doc recommended sidestepping the problem with N independent gateway processes fanned out by Caddy, since that needs zero Hermes patching. **That's ruled out**: we run on Render's `standard` plan (`render.yaml` — 1 container, 5 GB disk, no horizontal scale-out), and N full Hermes processes each carrying their own Python runtime, model routing, session store, and memory subsystem is real, additive RSS per agent — not something we can absorb for more than a couple of profiles on one instance. **Multiplexing — one process, N profiles — isn't a nice-to-have here, it's the only workable shape given the deployment target.** So the question this doc actually needs to answer is "what does it take to make LINE join Hermes's real multiplexing (Option A below)," not "how do we avoid needing it."

All source citations below are against the live `nousresearch/hermes-agent` install at `/opt/hermes` on our Render instance, confirmed via SSH — this is the actual pinned 0.19.0 source, not the marketing docs page.

---

## What multiplexing actually multiplexes

Three genuinely different mechanisms hide under one flag:

1. **Per-credential polling platforms** (Telegram, Discord, Slack, Matrix, Signal, …). Each profile supplies its own bot token; `_start_secondary_profile_adapters()` in `gateway/run.py` (~L9316) creates and connects a real, independent adapter instance per profile, under that profile's own `HERMES_HOME`/secret scope. This works exactly as documented and needs nothing from us.

2. **`webhook` and `api_server` — the only two platforms with real multi-tenant routing.** `gateway/platforms/webhook.py` registers *both* `/webhooks/{route_name}` and `/p/{profile}/webhooks/{route_name}` on the **same handler** (`webhook.py` ~L260-268), dispatching against a route table declared entirely inside `platforms.webhook.extra.routes` — each route carries its own secret and an optional `profile:` field. One adapter instance, N routes, each stamped with a different profile at request time. `api_server.py` does the analogous thing with a `_make_profile_prefix_middleware()` (~L1455) mounting `/p/{profile}{path}` routes. This is what the "reached via a `/p/<profile>/` URL prefix" language in the docs is actually describing — it was purpose-built for these two.

3. **Everything else in the port-binding list — `line`, `whatsapp_cloud`, `msgraph_webhook`, `feishu`, `wecom_callback`, `bluebubbles`, `sms`.** Each is a single-instance adapter: one credential pair, one fixed path, instantiated once (`adapter_factory=lambda cfg: LineAdapter(cfg)` — LINE's `plugin.yaml`). `LineAdapter.__init__` (`plugins/platforms/line/adapter.py` ~L864-870) reads `LINE_CHANNEL_ACCESS_TOKEN`/`LINE_CHANNEL_SECRET` via a single `os.getenv()` call each; `self.webhook_path = extra.get("webhook_path", DEFAULT_WEBHOOK_PATH)` where `DEFAULT_WEBHOOK_PATH = "/line/webhook"` is a scalar, not a list. There is no channel-list, no per-profile array, anywhere in `gateway/config.py` or `plugin.yaml` for LINE. It cannot represent more than one channel.

**The enforcement, not just the absence of a feature:** `_start_one_profile_adapters()` (`gateway/run.py` ~L9385-9420) loads a secondary profile's own `config.yaml` and checks it for any enabled port-binding platform *before* starting anything for that profile:

```
if port_binding_platforms:
    raise SecondaryPortBindingConfigError(
        f"Profile '{profile_name}' enables port-binding platform(s) "
        f"{joined}, but gateway.multiplex_profiles is on. The default "
        f"profile owns the single shared HTTP listener and serves every "
        f"profile through the /p/{profile_name}/ URL prefix. Remove "
        ...
    )
```

If a secondary profile's config enables `line`, that **entire profile** is skipped — not just LINE, every platform it configured — logged as a warning, gateway keeps running for everyone else. This is the trap: it's easy to reach for "just enable `line` on the secondary profile too" and get a silently dead agent instead of a partial failure.

**Bottom line:** with multiplexing on, LINE can only ever be configured on the one profile that owns the shared listener (in our deployment, always the default profile — it's the process `main-wrapper.sh` boots). Every other profile gets zero LINE, and if you try anyway, that profile gets zero of everything.

## What we found already live on our own instance

Worth documenting because it means our prior "verification" of multiplexing doesn't actually prove what the commit message claims:

- `render.yaml`/`scripts/patch-config.py` set `gateway.multiplex_profiles: true` (`f3fd11a`, `da0644f`), and `ARCHITECTURE.md` was updated to describe the multi-profile model — but it describes it as "each profile gets its own gateway process on a separate port," which is the **pre-multiplexing** one-gateway-per-profile model, not what the flag actually does (one shared process, no per-profile ports).
- `da0644f`'s commit message claims verification via "both `gateway-default` and `gateway-ngraph-agent` supervise processes running" — two separate supervised gateway processes is precisely the outcome real multiplexing is supposed to prevent (a secondary profile running its own gateway is a hard error once multiplexing is active, per upstream's own docs). So what was actually observed was the old model, not multiplexing working.
- Live state as of 2026-08-01 (`ssh srv-d97k2t57vvec73ccpg2g@ssh.oregon.render.com`): `hermes profile list` shows only `default`. `/opt/data/profiles/` contains only `profiles/default/`. `gateway_state.json` reports `"served_profiles":["default"]`. The `gateway-ngraph-agent` s6-supervise processes are still running (pids 270/271/273) but supervising nothing — no live gateway process under them, empty log file. Harmless orphaned state, but it means the "ngraph-agent" test profile referenced in the verification commit no longer exists and was never actually exercised under real multiplexing.
- `ARCHITECTURE.md` links to a `#deleting-a-profile-safely` anchor that doesn't exist anywhere in the file — broken doc reference, should either get a real section or the link removed.

None of this changes the verdict above (which comes from reading the enforcement code directly), but it means: don't treat that commit as evidence multiplexing was ever proven to work for a second profile on this container.

## Option A: patch Hermes for real multi-channel LINE

### The part that makes this cheaper than it first looks

The multiplexer already solves the *hard* half of this problem for every other platform — per-profile config/skills/memory resolution, session namespacing (`agent:<profile>:…`), and pairing-store isolation are all generic machinery in `gateway/run.py`, not LINE-specific code we'd have to write:

- `_start_secondary_profile_adapters()` already builds a `PairingStore` per served profile and stores it in `self.pairing_stores[profile]` (~L9375-9382), and `authz_mixin` already routes pairing checks to the right one.
- `_configure_profile_adapter()` already wraps a per-profile message handler (`_make_profile_message_handler(profile_name)`) that stamps `source.profile` before delegating into the shared turn-resolution path — this is the exact mechanism, already built and already tested, that makes config/skills/memory resolve correctly per profile.
- `_profile_runtime_scope(profile_home)` already exists as the context manager that switches secret/config scope for the duration of a call.

None of that needs to be reinvented. **The only genuinely new work is the credential/routing layer specific to LINE** — the thing a single-instance adapter doesn't have and the generic multiplexer plumbing can't give it for free:

1. **Config schema** — `platforms.line.extra.channels: [{profile, channel_secret, channel_access_token, webhook_path?}, ...]`, declared once on the **default profile** (so it never trips `SecondaryPortBindingConfigError` — no secondary profile ever enables `line` locally). Self-contained inside the LINE plugin's own config parsing, mirroring `webhook`'s `extra.routes` — no changes needed to `gateway/config.py`'s core `PlatformConfig` dataclass.
2. **N credential contexts instead of one** — `LineAdapter` currently holds a single `channel_access_token`/`channel_secret`/`_LineClient`. Needs a small per-channel struct (secret, token, its own `_LineClient` instance) so outbound replies go out on the right channel's token.
3. **Routing** — one route per channel, `/p/{profile}/line/webhook`, matching the URL convention Hermes already uses for `webhook`/`api_server`, rather than inventing a new dispatch scheme. Registered the same way `webhook.py` registers both its prefixed and unprefixed routes.
4. **Per-request handling** — on a hit to `/p/{profile}/line/webhook`, look up that channel's secret, verify the signature, then call the *same* `_make_profile_message_handler(profile_name)` pattern the polling-platform path already uses. This is genuinely new glue code, but it's gluing two things that both already exist (LINE's own event parsing, the multiplexer's profile-stamped handler) rather than building either from scratch.
5. **Per-channel overrides for what's currently a flat env var** — `LINE_ALLOWED_USERS`, `LINE_ALLOWED_GROUPS`, `LINE_DM_POLICY`, `LINE_HOME_CHANNEL`, `LINE_REQUIRE_MENTION`. These move from `os.getenv()` into the per-channel struct from (1).
6. **Media serving** (`/line/media/<token>/<filename>`) needs to keep resolving to the right channel's stored media/token.
7. **Interaction with our own two existing patches.** `line-dm-pairing.patch` and `line-group-mention.patch` already modify `__init__`, signature verification, and message dispatch in this same file. This patch has to be built *on top of* both (patch order already matters per `ARCHITECTURE.md`) — realistically as one combined, restructured patch rather than three independent ones, since all three touch overlapping hunks.

### Effort and upstream fit

Narrower than the first pass of this doc estimated, now that the reusable multiplexer plumbing is accounted for — but still real: a per-channel credential/client model, per-channel route registration, signature dispatch, and a merge with two existing patches touching the same hunks. Call it a solid multi-day build plus test coverage (`tests/test_render_mention.py`-style unit tests won't cover this; it needs its own), not a "~20 line call-out" like `line-group-mention.patch`. It's a real, permanent addition to what `scripts/upgrade-preflight.sh` has to track on every future Hermes bump — meaningfully more than today, but not the open-ended redesign the first draft implied.

**Is it a good candidate for a PR to Nous?** Still probably not, for the same reason as before: it's a new multi-tenancy capability, not a gap-fill, and it's the same *category* of ask as [#527](https://github.com/NousResearch/hermes-agent/issues/527)/[#34352](https://github.com/NousResearch/hermes-agent/issues/34352), both sitting in `needs-decision` for months with no maintainer response, because Hermes' identity is a personal, single-owner agent and most self-hosted users have exactly one LINE channel. Worth *filing* as an issue/RFC regardless — it costs nothing, and if a maintainer is receptive it saves us from maintaining the patch at all — but plan for "we carry this ourselves," not "this merges."

**Verdict: buildable, moderate (not huge) effort, worth doing given the Render memory constraint rules out the alternative.** This is now the recommended path — see below.

## Option B: Caddy + independent gateway processes — ruled out

**Not viable given our deployment target.** We run one `standard`-plan Render container (`render.yaml`) with no horizontal scale-out — one CPU/RAM budget for the whole business's worth of agents. Each independent Hermes gateway process carries its own Python runtime, model-routing layer, session store connection, and memory subsystem; that's real, additive RSS per agent, not something that amortizes the way N lightweight connections would. This works fine for one or two profiles but doesn't scale to "N agents per client" as a general pattern, which is the actual goal. Documented below for completeness since it's still the right call *if* we ever move to a deployment shape with real headroom per agent (a bigger plan, or genuinely low agent counts per client) — but it's not the answer for the stated constraint.

<details>
<summary>What it would have looked like</summary>

This sidesteps the whole problem rather than solving it inside LINE's adapter, using a capability Hermes already ships natively: **one full gateway process per profile**, the model that predates multiplexing (`ARCHITECTURE.md` originally documented this at the top of the multi-profile section — "each profile gets its own `/run/service/gateway-<name>` service... per-profile settings (including port-binding platforms like LINE) stay independent"). Multiplexing's `SecondaryPortBindingConfigError` only fires inside the multiplexer's own secondary-profile startup path — it's simply not in play if each profile runs as its own independent process instead of being served by one shared listener.

**Shape of the solution:**

1. **Turn `gateway.multiplex_profiles` back off.** It buys us nothing for this use case and actively forbids the thing we want (a secondary profile owning LINE). Revert `f3fd11a`/`da0644f`'s config, or at minimum stop relying on it for LINE.
2. **Each client agent = its own profile, its own full gateway process**, installed the standard way (`hermes -p <profile> gateway install`), each with a distinct `LINE_PORT` in its own `.env` (default 8646, then 8647, 8648, …) and its own `LINE_CHANNEL_SECRET`/`LINE_CHANNEL_ACCESS_TOKEN`. Fully independent — separate crash domains, separate memory, exactly the "hard process-level isolation" upstream's own docs recommend when you're not using multiplexing.
3. **Caddy does the fan-out**, extending `caddy/Caddyfile` from its current single-backend form:
   ```
   handle /line/* {
       reverse_proxy 127.0.0.1:{$LINE_PORT:8646}
   }
   ```
   to a per-profile prefix, using `handle_path` (strips the prefix, unlike the current `handle` which preserves it — deliberate, since each backend still expects to see its own `/line/webhook`):
   ```
   handle_path /line/coder/* {
       reverse_proxy 127.0.0.1:8647
   }
   handle_path /line/research/* {
       reverse_proxy 127.0.0.1:8648
   }
   ```
   Each channel's webhook URL registered in *that* channel's LINE Developers Console entry becomes `https://<host>/line/<profile>/webhook`.
4. **`LINE_PUBLIC_URL` per profile** needs to match its own prefix (`https://<host>/line/<profile>`) so outbound media URLs the adapter generates round-trip through the right Caddy route.
5. **Boot-time reconciliation** — extend `ARCHITECTURE.md` Pattern 2 (cont-init hook) so container boot brings up N profile gateway services, not just the default one, the same idempotent way `scripts/bootstrap.sh` already handles the default.
6. **`admin-tools/env-sync`** needs to manage N credential sets per client instead of one — a real but bounded extension of the existing tool, not a new mechanism.

**Effort: config, Caddyfile, and a boot script — zero Hermes source changes.** Everything here uses patterns already proven in this repo (Pattern 1/2 from `ARCHITECTURE.md`), and it composes cleanly with the two LINE patches we already carry, since each profile's LINE adapter is still a normal, single-channel `LineAdapter` — nothing about its internals changes.

</details>

## The bigger picture

This whole limitation is another concrete instance of the mismatch [`02-nous-platform-risk.md`](02-nous-platform-risk.md) already named: **Hermes is architected as a personal agent, and we keep needing it to behave like a multi-tenant business platform.** Multiplexing's LINE gap is small and specific, but it's the same shape as [#527](https://github.com/NousResearch/hermes-agent/issues/527)/[#34352](https://github.com/NousResearch/hermes-agent/issues/34352) — a capability that would need to be *decided* upstream, not just coded. [`06-recommendation.md`](06-recommendation.md)'s "own the gateway, rent the agent" plan (build `ngraph-gateway` as our own LINE + identity service driving Hermes as a stateless completion engine) would dissolve this problem entirely, since we'd own the webhook fan-out ourselves rather than asking Hermes' adapter to do it — the Caddy approach above is effectively a cheap, partial version of that same idea, scoped to just the webhook layer.

## Recommendation

**Revised 2026-08-01** given the Render `standard`-plan memory/CPU constraint rules out running N full gateway processes per client:

1. **Build the Option A patch.** Given the constraint, this is no longer "build a thick patch vs. use free config" — it's the only path that gets N LINE-connected agents onto one gateway process at all. The effort is real but bounded: the multiplexer's per-profile config/session/pairing plumbing already exists and doesn't need to be rebuilt, so the patch is scoped to LINE's credential/routing layer specifically (per-channel `_LineClient` + secret, `/p/{profile}/line/webhook` routes, dispatch into the existing profile-stamped message handler). Plan for it to merge with, or replace, our two existing LINE patches rather than stacking as a third independent one.
2. **File it upstream as an issue/RFC in parallel, but build assuming it stays ours.** Costs nothing to ask; the precedent ([#527](https://github.com/NousResearch/hermes-agent/issues/527)/[#34352](https://github.com/NousResearch/hermes-agent/issues/34352) sitting in `needs-decision` for months) says don't block the client-facing timeline on a merge.
3. **Keep `gateway.multiplex_profiles: true`** — it's the correct setting for this deployment shape now, unlike the first draft's conclusion. What still needs fixing regardless of which option we pick: the orphaned `gateway-ngraph-agent` s6 state left from the earlier stray test profile, `ARCHITECTURE.md`'s multi-profile section (it currently describes the pre-multiplexing one-process-per-profile model, not what the flag actually does — worth correcting so it doesn't mislead the next person), and the broken `#deleting-a-profile-safely` anchor.
4. **Measured, not assumed:** one idle Hermes gateway process on the live instance is **~262 MB RSS** (`ps -o rss -p 6463` → 267,868 KB), against a **2 GiB** cgroup ceiling (`/sys/fs/cgroup/memory.max` → 2147483648 bytes — confirms the Render `standard` plan). That's ~13% of the container's entire memory budget for one *idle* agent, before Caddy, the dashboard process, s6, OS overhead, or any load-time growth (active sessions, tool execution, memory indexing) are accounted for. Four or five profiles as independent processes would plausibly exhaust the container before any of them are doing real work. This confirms Option B was correctly ruled out — it isn't a close call.

---

### Sources

- Hermes 0.19.0 source (verified via SSH on the live Render instance, `/opt/hermes`): `gateway/run.py` (~L9316-9420, `_start_secondary_profile_adapters`/`_start_one_profile_adapters`/`SecondaryPortBindingConfigError`), `gateway/config.py` (`PORT_BINDING_PLATFORM_VALUES`, ~L384-413), `gateway/platforms/webhook.py` (~L260-268, dual route registration), `gateway/platforms/api_server.py` (~L1455, `_make_profile_prefix_middleware`), `plugins/platforms/line/adapter.py` (~L841-1045, 1990), `plugins/platforms/line/plugin.yaml`.
- This repo: `f3fd11a` (enable gateway multiplexing), `da0644f` (sync config at boot; multi-profile architecture doc), `bc1c0b4`, `caddy/Caddyfile`, `ARCHITECTURE.md`.
- [`02-nous-platform-risk.md`](02-nous-platform-risk.md), [`06-recommendation.md`](06-recommendation.md) — the personal-agent-vs-multi-tenant mismatch this problem is another instance of.
- [LINE Developers Console overview](https://developers.line.biz/en/docs/line-developers-console/overview/) — up to 100 channels per provider (external LINE limit, not a Hermes constraint).
- Hermes issues [#527](https://github.com/NousResearch/hermes-agent/issues/527), [#34352](https://github.com/NousResearch/hermes-agent/issues/34352).
