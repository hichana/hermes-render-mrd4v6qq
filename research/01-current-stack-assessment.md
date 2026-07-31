# 01 — Current stack assessment

What we actually depend on in Hermes, what that coupling costs, and which parts of the cost are structural versus already-paid.

---

## What we use Hermes for

Reading this repo end to end, the Hermes surface we genuinely depend on is narrower than it looks:

| Hermes capability | Do we use it? | Notes |
|---|---|---|
| LINE adapter (webhook, reply tokens, group/DM events) | **Yes — critical** | The one thing that would be expensive to replace |
| Gateway process + s6 supervision | Yes | Replaceable by any long-running web service |
| Session store (FTS5 SQLite) + session keying | Yes | Per-user in DMs, `group_sessions_per_user` in groups |
| Dashboard (API-key UI, file browser, TUI chat) | Yes — operator convenience | Not client-facing; we've had to gate it behind basic auth and Caddy |
| Honcho memory integration | Not yet | Planned (`plans/hermes-plan.md` Phase 3); Honcho itself is stack-agnostic |
| Skills system | Barely | One skill: `skills/line-invite/` |
| Terminal / `execute_code` / file tools | **No — actively disabled** | `plans/hermes-plan.md` Phase 4 disables these for every employee-facing profile |
| Cron / scheduled tasks | No | Available, unused |
| Desktop app, TUI, CLI chat | No | Client-facing surface is LINE only |
| Other 20+ platform adapters | No (optionally later) | Telegram token was found live on the volume, untracked — see `ARCHITECTURE.md` |
| Self-improvement / skill learning | No | Would be a governance risk in a client instance anyway |
| Sub-agent delegation | No | |

**The finding that matters:** the headline reason to pick Hermes — "batteries-included, full-featured, actively improved, and those improvements flow through to our clients" — is mostly not reaching our clients. The batteries we ship are the LINE adapter and a session store. Everything else is either disabled on purpose, unused, or an operator convenience for us.

This is not an argument that Hermes was the wrong choice. It was a fast, correct choice for getting a client-facing agent live. It's an argument that the *ongoing* value we're extracting is much smaller than the ongoing cost, and that ratio is what a rebuild decision turns on.

## The coupling surface

We reach into upstream internals in five places. `scripts/upgrade-preflight.sh`'s `DEPS`/`SYMBOLS`/`STRUCTURE` tables are the maintained inventory:

1. **`patches/line-dm-pairing.patch`** (451 lines) — DM pairing policy, `LineInviteStore`, `_LineClient.get_profile`, invite redemption, `plugin.yaml` entries.
2. **`patches/line-group-mention.patch`** (86 lines) — thin call-outs into our own module. Deliberately thin so drift breaks loudly.
3. **`patches/line-dm-pairing.tests.patch`** (388 lines) — not applied to the image; upstream-PR and regeneration aid.
4. **`modules/line/render_mention.py`** — `COPY`'d into the upstream package as a sibling module. Importable only because the plugin dir is a real package on Hermes' editable install — a property of upstream's packaging, not of our file.
5. **Boot-time config mutation + cont-init hook** — `scripts/patch-config.py`, `scripts/bootstrap.sh`, plus image-shape assumptions (s6-overlay, no `ENTRYPOINT` override, `/opt/hermes/.venv`, `gosu` absent).

That is roughly **925 patch lines plus a module plus a boot hook**, all of which must re-apply against an upstream that ships on the order of 180 commits a week.

## What the upgrade tax actually costs

Worth being precise, because it's easy to over- or under-state:

**The one-time disaster already happened.** The `v2026.5.7` → `v2026.7.7.2` bump was a bare one-line tag edit. Upstream had moved to s6-overlay, repointed `/usr/bin/tini`, and dropped `gosu`. The container booted wedged and stayed that way **8 days** while `healthCheckPath: /api/status` — served by the dashboard — kept answering 200. Six commits to clean up.

**The tooling built in response is genuinely good, and it has bounded the cost:**

- `scripts/upgrade-preflight.sh` — ~1 minute, no Docker, no clone. Closed-world drift check with a maintained manifest.
- `tests/test_preflight_manifest_coverage.py` — makes the manifest self-policing for mechanically-detectable dependencies.
- `scripts/smoke-test.sh` — ~33s warm, 9–10 assertions against live container state rather than log lines, under Render's actual runtime restrictions.
- `UPGRADING.md` — a 5-phase procedure with a worked example.
- `admin-tools/env-sync` — verified-restart env management, because `hermes gateway restart` is a silent no-op on this deployment.

**Steady-state cost per bump** (from the `v2026.7.20` run, verified 2026-07-30): preflight found 1 blocker and 6 to review; both patches applied unchanged; 99 unit tests green; 9 smoke assertions green. Call it **half a day of attention per bump**, most of it reading, plus deploy and Phase 4 live verification.

So the honest read: **the patch tax is real but no longer the emergency it was.** It is roughly half a day per upgrade plus the standing cost of maintaining ~1,400 lines of tooling and ~15KB of procedure documentation whose entire purpose is to survive somebody else's release cadence.

The sharper version of the complaint isn't "patching is expensive." It's: **we built a small, high-quality engineering organization around not being surprised by an upstream that doesn't know we exist.** That investment is sunk and it works. The question is whether we want to keep paying its maintenance in exchange for a feature set we've mostly disabled.

## Where Hermes structurally fights us

These are not bugs and they will not be fixed by an upgrade.

### Identity is a personal-agent identity

Hermes' model is one agent with one memory belonging to one person, reachable from many surfaces. Our model is one agent serving many employees of one business, each of whom must not see the others' context. Everything downstream follows from that mismatch:

- **Memory.** The built-in memory tool writes a single global store with no concept of who said what ([#11430](https://github.com/NousResearch/hermes-agent/issues/11430), P2, opened 2026-04-17, unassigned). `group_sessions_per_user: true` isolates *sessions*, not *memory*.
- **The off-switch is broken.** `plans/hermes-plan.md` Phase 3 already records it: even with `memory_enabled: false` and `user_profile_enabled: false`, the built-in memory prompt may still be auto-injected (Hermes issues #45422 / #18404). So "just turn it off and let Honcho own memory" is not yet a clean move.
- **Permissions are binary.** Authorized or blocked, with identical capabilities for everyone authorized — same tools, same slash commands, same terminal access ([#527](https://github.com/NousResearch/hermes-agent/issues/527), P2, `needs-decision`, opened 2026-03-06, no maintainer response, unassigned).
- **Tenancy isn't a boundary.** [#34352](https://github.com/NousResearch/hermes-agent/issues/34352) ("Solving the Multi-Tenant Hermes Problem", opened 2026-05-29) reports memory operations bypassing the hook system entirely, with a cited real-world incident of private-context leakage into public output. Also `needs-decision`, also unassigned. The issue notes it spans 12+ related open issues.

Our mitigations are real but they are all *conventions*: one Render service per business for hard isolation, one profile per virtual employee, `userPeerAliases` mapping platform IDs to distinct Honcho peers, terminal tools not exposed. Conventions hold until an upstream default changes underneath them — and Phase 0 of `plans/hermes-plan.md` is a list of behaviors we've had to write down as "verify this yourself, the docs don't confirm it."

### Identity is keyed on the platform, not the user

`userPeerAliases` maps **platform runtime IDs** (LINE user ID, Slack user ID) to peer names. That's the mechanism. A custom UI has no platform runtime ID to map.

`plans/api-server-identity-plan.md` already names this: the OpenAI-compatible API server carries identity completely differently — the caller asserts it — and whether that yields distinct Honcho peers is **unverified**. `plans/hermes-plan.md` Phase 0 escalates it to a blocker: *"don't put real client users behind it until it's settled."*

Given "custom UI is coming," this is the load-bearing finding in this document. It is the one gap where the answer might be "Hermes cannot do this," and we don't currently know.

### Multi-employee means multi-LINE-channel

Per `plans/hermes-plan.md` Phase 2: Hermes locks a LINE channel token to one profile, so every virtual employee needs its own LINE Official Account provisioned in the LINE Developers console. That's a per-employee external provisioning step we can't automate away inside Hermes, and it makes "add a virtual employee" a manual, multi-system operation.

`gateway.multiplex_profiles: true` (enabled 2026-07-31, commit `f3fd11a`) lets one gateway process route across profiles, which removes the process-per-profile overhead. It does not change the token-to-profile binding or the identity model.

## What's genuinely good and shouldn't be thrown away

- **Per-business container isolation on Render.** The strongest boundary in the design, it's independent of Hermes, and it survives any framework change.
- **`admin-tools/env-sync`.** Framework-agnostic. The verified-restart discipline and the `local ⊆ remote` invariant are hard-won and reusable.
- **`modules/line/render_mention.py`.** Already ours, already unit-tested with no upstream clone, already the substantive half of the mention logic. In a rebuild this is a starting asset, not a loss.
- **The operational knowledge in `ARCHITECTURE.md` / `HISTORICAL-GOTCHAS.md`.** The `.env`-wins precedence rule, the restart-verification requirement, the "assert state not log lines" principle — none of that is Hermes-specific.
- **`plans/hermes-plan.md` itself.** It is, in effect, a requirements specification for a multi-tenant business agent platform that happens to be written as a Hermes configuration checklist. It transfers.
