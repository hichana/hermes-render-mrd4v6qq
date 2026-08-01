# 08 — Scoping the LINE multi-channel patch, and its place in `UPGRADING.md`

**Date:** 2026-08-01
**Follows:** [`07-line-multi-channel-multiplexing.md`](07-line-multi-channel-multiplexing.md), which concluded — given the ~262 MB RSS per idle gateway process measured against a 2 GiB container ceiling — that a real, single-process, multi-channel LINE patch is the only workable path, not a nice-to-have.
**This doc:** a concrete design for that patch (file by file, hunk by hunk, at the level `patches/README.md`'s own regeneration recipe operates at), and how it slots into `UPGRADING.md`'s phases going forward. Not a finished diff — a scope precise enough to build from and to size honestly.

**Headline revision from `07`:** the effort estimate there was conservative. Reading the actual multiplexer plumbing in `gateway/run.py` and `gateway/platforms/base.py` turned up a mechanism that does almost all of the hard part for us — see "The load-bearing discovery" below. And by following `line-group-mention.patch`'s own proven shape (thin adapter call-outs, substantive logic in a testable sibling module), this can stay closer to that patch's size than to a from-scratch rewrite.

---

## The load-bearing discovery

Every inbound LINE message reaches the gateway through exactly one call site, in `_handle_message_event` (and its postback sibling), in the *already-patched* `plugins/platforms/line/adapter.py`:

```python
source_obj = self.build_source(
    chat_id=chat_id,
    chat_type=chat_type,
    user_id=user_id,
    user_name=user_id,
    chat_name=chat_id,
)
event_obj = MessageEvent(..., source=source_obj, ...)
await self.handle_message(event_obj)
```

`build_source()` (`gateway/platforms/base.py`) returns a `SessionSource` (`gateway/session.py`) with a **mutable `.profile` field**. Nothing downstream cares how that field got set — `_resolve_profile_home_for_source()` in `gateway/run.py` (the function that decides which profile's `HERMES_HOME`, config, skills, and memory a turn runs against) documents its own resolution order as:

> 1. `source.profile` — set by `/p/<profile>/` URL prefix, per-credential adapter ownership, **or `profile_routes` matching at `build_source` time**.

`/p/<profile>/` URL-prefix stamping and `profile_routes` guild/channel matching are just the two mechanisms upstream happened to build. The field itself is generic. The existing polling-platform multiplex path proves this even more directly — `_make_profile_message_handler()` (`gateway/run.py` ~L9676) is a thin wrapper that does exactly one thing: `if not event.source.profile: event.source.profile = profile_name`, then calls the normal handler.

**So the entire "make the agent turn resolve to the right profile" problem — config, skills, memory, session namespace, pairing store selection — is already solved, generically, by one field on an object we already construct.** The only new work is: know which profile a given inbound LINE webhook belongs to, and set that one field before `self.handle_message(event_obj)` runs. Nothing about `_resolve_profile_home_for_source`, session-key namespacing, or per-profile pairing stores needs to change.

## Architecture: thin call-outs + a testable sibling module

Follow `line-group-mention.patch`'s own template rather than inventing a new shape: put the substantive logic in a new sibling module, `modules/line/line_multiplex.py`, `COPY`'d into the image next to the existing `modules/line/render_mention.py` — unit-testable with no upstream clone, and the actual patch hunk stays small and legible.

**What lives in `line_multiplex.py`:**

```python
@dataclass
class LineChannel:
    profile: str
    channel_secret: str
    channel_access_token: str
    allowed_users: set[str]
    allowed_groups: set[str]
    allowed_rooms: set[str]
    allow_all: bool
    dm_policy: str
    home_channel: str
    client: "_LineClient"          # one bearer-token client per channel

class ChannelRegistry:
    """Holds every configured LineChannel and the current-request context."""
    def __init__(self, channels: dict[str, LineChannel]) -> None: ...
    def by_profile(self, profile: str) -> LineChannel | None: ...
    def by_webhook_path(self, path: str) -> LineChannel | None: ...

_current: contextvars.ContextVar["LineChannel"] = contextvars.ContextVar("line_current_channel")

def set_current(channel: LineChannel) -> contextvars.Token: ...
def current() -> LineChannel: ...
```

**Why `contextvars`, not a plain instance attribute set-and-restore:** `_handle_webhook` processes a batch of events from one request sequentially, but the aiohttp server handles *concurrent* requests from *different channels* interleaved on the same event loop. A naive `self._active_channel = X; ...; self._active_channel = None` has a real TOCTOU race the moment two channels' webhooks are in flight at once — request B's `finally` block could clear the context request A is still mid-await on. `contextvars.ContextVar` is specifically designed for this: each `asyncio.Task` gets its own copy of the context (including the `asyncio.create_task()` call for the typing-indicator ping in `_handle_message_event`, which needs to see the *right* channel's client, not whichever channel's request happened to run last). This is the one piece of the design that has to be right, not just convenient.

**What stays in the `adapter.py` patch (thin):**

1. `__init__` — parse `extra.get("channels")` (a list) into `LineChannel` entries, plus register the adapter's own top-level `LINE_CHANNEL_SECRET`/`LINE_CHANNEL_ACCESS_TOKEN`/etc. as one more `LineChannel` (the default profile's own channel) so single-channel deployments — every instance we run today — need zero config changes and hit the exact same code path as a multi-channel one with one entry.
2. `channel_secret`, `channel_access_token`, `allowed_users`, `allowed_groups`, `allowed_rooms`, `allow_all`, `_dm_policy` — convert from plain instance attributes (set once in `__init__`, read everywhere else unchanged) to one-line properties delegating to `line_multiplex.current()`. **This is the reason the rest of the file — `_dispatch_event`, `_handle_message_event`, `_handle_postback_event`, `_try_handle_invite_redemption`, `send()`, the mention-gate call sites added by the other two patches — needs no further changes.** They already read `self.channel_secret`/`self.allowed_users`/etc.; they just now resolve per-request instead of per-process.
3. `setup()` — instead of registering one route at `self.webhook_path`, loop `self._channels.registry` and register one route per channel: the default channel keeps the existing unprefixed `/line/webhook` (so nothing changes for a live single-channel instance's already-configured LINE console callback URL), every other channel gets `/p/{profile}/line/webhook`.
4. `_handle_webhook` — at the top, resolve which channel this request's route matched (`request.match_info.get("profile")`, or the default channel for the unprefixed route), call `line_multiplex.set_current(channel)` for the duration of the request, verify the signature against *that* channel's secret (this replaces the single `self.channel_secret` reference — becomes free once (2) lands), then proceed exactly as today.
5. `_handle_message_event` / `_handle_postback_event` — **one line added** after the existing `source_obj = self.build_source(...)` call: `source_obj.profile = line_multiplex.current().profile`. This is the entire "make the turn resolve to the right agent" mechanism, per the discovery above.
6. `plugin.yaml` — document the new `channels` structured-config key (not a flat env var — it's a list, so it has to live in `config.yaml` under `platforms.line.extra.channels`, not `.env`; flag this explicitly since every other LINE knob today is env-var driven and this one deliberately isn't).

## What's deliberately deferred to a v2, and why that's safe for v1

- **`LineInviteStore` and the central `PairingStore()` call in `_try_handle_invite_redemption`** (from `line-dm-pairing.patch`) stay scoped to the default profile's own `HERMES_HOME` in v1, rather than becoming per-channel. This is not a security gap: LINE user/group IDs are channel-scoped by LINE itself (the same physical person gets a different `U`-prefixed ID per channel), so there's no cross-channel collision risk in a shared invite/approval store — it's purely an audit-organization wrinkle (every channel's pending invites and approved users list in one JSON file instead of split per profile). Real fix for v2: call `PairingStore(profile=line_multiplex.current().profile)` and run `LineInviteStore()`'s directory resolution under `_profile_runtime_scope(profile_home)`, mirroring what `_start_secondary_profile_adapters()` already does for `self.pairing_stores[name]`.
- **`MentionGate`** (from `line-group-mention.patch`) stays a single shared instance across all channels in v1, for the same reason — its internal state is keyed by `chat_id`/`user_id`, which LINE already scopes per channel, so no cross-tenant leakage even sharing one instance.
- **Media serving** (`/line/media/<token>/<filename>`) stays unprefixed and shared across channels — the token itself (a UUID) is the lookup key, not the URL prefix, so there's nothing to disambiguate. `LINE_PUBLIC_URL` per channel isn't needed either, as long as the media path resolves from a single shared token cache (it already does).

Both v1-deferred items are two-line changes later, not architectural gaps — worth stating plainly so "v1" doesn't get read as "insecure."

## Interaction with the two patches already in `patches/`

This has to be designed and generated against a tree with **both** `line-dm-pairing.patch` and `line-group-mention.patch` already applied — same rule `patches/CLAUDE.md` states for the existing pair, extended to three:

- `__init__` is the collision point. `line-dm-pairing.patch` adds `self._dm_policy = ...` and `self._invites = LineInviteStore()`; `line-group-mention.patch` adds `self._mention_gate = render_mention.MentionGate()`. The multi-channel patch's `__init__` hunk lands after both, and specifically **replaces** the `self._dm_policy = ...` assignment (turns it into the property in step 2 above) rather than adding near it — meaning this patch can't be purely additive against the dm-pairing patch's hunk; regenerating it means re-diffing `__init__` as a whole, not stacking three independent context blocks.
- `_dispatch_event`'s allowlist-gate block (rewritten by `line-dm-pairing.patch` into the `src_type == "user"` branch) references `self.allowed_users`/`self.allow_all` — untouched by the property conversion, since those are still spelled the same way, just resolved differently.
- Net effect: **apply order becomes dm-pairing → group-mention → multi-channel**, and the multi-channel patch is the one most likely to need hand-regeneration (not just re-application) the next time any of the three drifts, since it's the one touching the most shared surface (`__init__`).

## Effort, revised

Closer to `line-group-mention.patch`'s shape than the "several hundred lines across the whole adapter" estimate in `07` — the module holds the real complexity (`LineChannel`, `ChannelRegistry`, the contextvar, per-channel `_LineClient` construction), the adapter-side hunk is mostly one-line conversions (attribute → property) plus the route-registration loop and the one-line profile stamp in two places. Still a genuinely new module with its own test file (`tests/test_line_multiplex.py`, mirroring `tests/test_render_mention.py`) covering: channel resolution by path, signature isolation between channels (a request signed with channel A's secret must fail against channel B's route), and the default-channel backward-compat path. Call it comparable in *shape* to the group-mention patch, larger in *size* because credential/routing logic is inherently more surface than a gate check — a multi-day build with tests, not a multi-week rewrite, and not the open-ended redesign the first pass of `07` implied.

---

## Where this lands in `UPGRADING.md`

### Phase 0 (release notes) — no change
This patch isn't triggered by a version bump. But once it exists, Phase 0's release-notes read gains a new thing to watch for: any upstream change to `SessionSource`, `build_source()`, or `profile_routes` matching becomes directly relevant to us in a way it isn't today, since we'd now depend on that mechanism from a third, independent call site (LINE) rather than just the two upstream already built it for.

### Phase 1 (preflight) — new manifest entries in `scripts/upgrade-preflight.sh`

New `DEPS` entries:
```
"modules/line/line_multiplex.py|blocker|our own module, not upstream — but its unit tests assert against gateway/session.py and gateway/platforms/base.py shapes below"
"gateway/session.py|review|SessionSource.profile field — multi-channel patch's core mechanism"
"gateway/platforms/base.py|review|build_source() — multi-channel patch calls it unmodified, then sets .profile on the result"
"gateway/pairing.py|review|PairingStore(profile=...) constructor kwarg — used if/when invite/pairing scoping moves to v2"
```
(`gateway/pairing.py` and `gateway/platforms/base.py` are already tracked as `review` for the other two patches — these are additive notes on existing rows, not new rows, except `gateway/session.py` which is genuinely new.)

New `SYMBOLS` entries:
```
"gateway/session.py|class SessionSource\b|SessionSource (multi-channel profile stamping)"
"gateway/platforms/base.py|def build_source\(|build_source() (multi-channel patch's call site)"
"gateway/pairing.py|profile: *(Optional\[str\]|str) *= *None|PairingStore(profile=...) kwarg"
```
And the existing `plugins/platforms/line/adapter.py|blocker` row's note extends to `"patch target: line-dm-pairing.patch + line-group-mention.patch + line-multi-channel.patch"`.

Per `tests/test_preflight_manifest_coverage.py`'s own rules (`ARCHITECTURE.md`, "Keeping the preflight manifest honest"), the new patch's `+++ b/...` targets and the new `COPY modules/line/line_multiplex.py ...` Dockerfile line are exactly what that coverage test derives its expectations from — so this patch, once it exists, has to land *with* its manifest entries in the same commit, or `pytest` goes red by design. That's the safety net working as intended, not extra work to route around.

### Phase 2 (bump and build) — extends, doesn't change, the existing recipe

- The `git apply` chain check in `patches/README.md` extends from two patches to three, same order rule: `line-dm-pairing.patch && line-group-mention.patch && line-multi-channel.patch`.
- `python3 -m pytest tests/ -q` gains `tests/test_line_multiplex.py`.
- `./scripts/smoke-test.sh` gains at least two new assertions, in the same "assert live state, not a log line" spirit as its existing ten:
  - A second registered route exists and behaves correctly: POST to `/p/<test-profile>/line/webhook` with a bad signature returns 401 (proves the route registered and signature verification runs against the *right* channel), not 404 (would mean the route never registered at all).
  - Cross-channel signature isolation: a payload signed with channel A's secret, POSTed to channel B's route, is rejected — this is the one property that, if it silently broke, would mean one client's channel could forge messages into another client's agent. Worth a dedicated assertion, not just inferred from the two patches' own unit tests.

### Phase 3/4 (deploy, verify) — one new item in the live-instance checklist

Add to the LINE round-trip verification (`UPGRADING.md` Phase 4, item 5): with two or more channels configured, confirm a message on **each** channel reaches the correspondingly-profiled agent (distinct reply, distinct memory) — not just that the default channel still works. The existing checklist is written for a single LINE channel; this patch is the first time "the default channel still works" stops being sufficient evidence that LINE is healthy.

### Phase 5 (close the loop) — the actual filing work

- `patches/README.md` — add `line-multi-channel.patch` to the "Files" section (module, patch, and any accompanying `.tests.patch`), and update the "Order is load-bearing" note to name all three patches.
- `patches/CLAUDE.md` — same three-patch update to its ordering rule.
- `ARCHITECTURE.md` — the "Adapters and patches" section's "Currently patched" list gains a third entry; the "How env vars work here" section gains a note that `platforms.line.extra.channels` is config.yaml-only, not `.env`-representable, which is a first for this repo's LINE surface.
- `admin-tools/env-sync` — extend to manage N credential sets and write the `channels` list into `config.yaml` (today it only manages `.env`; this is genuinely new scope for that tool, flagged already in `07`'s Option A effort list).
- File the upstream issue in parallel (per `07`'s recommendation) — costs nothing, don't block on it.
- Update `research/07-line-multi-channel-multiplexing.md`'s status once this patch exists and is verified live, the same way `06-recommendation.md` gets a "Revised" banner rather than a silent rewrite when its own conclusion changes.

---

### Sources

- Hermes 0.19.0 source (verified via SSH, `/opt/hermes`): `plugins/platforms/line/adapter.py` (`_handle_webhook` ~L1117, `_dispatch_event` ~L1147, `_handle_message_event` ~L1335, `_LineClient` ~L633-730), `gateway/platforms/base.py` (`build_source()` ~L5678-5745), `gateway/run.py` (`_make_profile_message_handler` ~L9676, `_resolve_profile_home_for_source` ~L18820-18860, `_profile_name_for_source` ~L18777-18820), `gateway/session.py` (`SessionSource`), `gateway/pairing.py` (`PairingStore`, profile-scoping docstring ~L235-250).
- This repo: `patches/line-dm-pairing.patch`, `patches/line-group-mention.patch`, `patches/README.md`, `patches/CLAUDE.md`, `scripts/upgrade-preflight.sh` (manifest format), `UPGRADING.md`.
- [`07-line-multi-channel-multiplexing.md`](07-line-multi-channel-multiplexing.md) — the decision this scopes.
