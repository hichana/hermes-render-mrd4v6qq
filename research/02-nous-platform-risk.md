# 02 — Is the Nous OSS-deprioritization worry justified?

The stated worry: *Nous is actively promoting their cloud service, login, and ecosystem. The open-source part could be deprioritized, or bloated with things needed for their own offerings, and that becomes our burden too.*

Verdict: **the worry is reasonable, the specific mechanism is somewhat mis-aimed, and the risk that actually threatens us is a different one that's already visible.**

---

## What Nous is actually selling

**Nous Portal** launched 2026-04-27. It bundles model access — 300+ models — plus tools (web search, image generation, TTS, browser use) and credits, at $0 free tier / $20 per month Plus. **Hermes Cloud** runs a hosted Hermes instance on a dedicated cloud instance, billed hourly from Portal credit.

The agent software itself remains MIT-licensed with no paywalled features, no per-seat fee, and no subscription requirement.

Structurally this is **model-and-tool resale plus managed hosting**, not a crippled-core play. The commercial product doesn't compete with the OSS agent; it competes with us going to OpenRouter directly and with us running our own Render service.

## Evidence on the OSS side

- ~60,000 GitHub stars within two months of the February 2026 launch; described as the fastest-growing OSS project in the agent space in 2026.
- Release cadence on the order of 180 commits a week — the reason `UPGRADING.md` exists at all.
- Upstream is still shipping to the parts we depend on: the `v2026.7.20` preflight found real, ordinary maintenance activity (an `hmac.compare_digest` fix in the LINE adapter), not abandonment.
- The Honcho integration is first-party and documented, with `HONCHO_API_KEY` in the standard env surface.

By the usual signals, this is a **healthy, extremely active open-source project**, not one being hollowed out.

## Where the worry is well-aimed

**Bloat is the real form of the risk, and it's already happening.** The concern that the OSS repo gets "bloated with things needed for their own offerings" doesn't require any bad faith from Nous — it's the default outcome of a fast-moving project with a commercial hosted product. The costs land on us as:

- **Surface area we must defend against.** We already disable terminal and `execute_code`, gate the dashboard behind Caddy and basic auth, and assert in `scripts/smoke-test.sh` that no Render MCP entry reappears in a client instance's config. Every new upstream capability is a new thing to audit and possibly switch off in a client-facing deployment. Our smoke test has a check whose entire job is asserting the *absence* of a capability.
- **Churn against our patch surface.** 180 commits/week against files we patch. Bounded today by preflight and the thin-patch design, but the trend is one-directional.
- **Config defaults drifting under a live instance.** `/opt/data/config.yaml` persists across deploys and *inherits* upstream defaults it doesn't explicitly set. The `v2026.7.20` bump flipped `session_reset.mode` from `both` to `none` — a behavior and cost change to a client instance from a release we didn't ask for. `UPGRADING.md` Phase 4 exists to catch exactly this class, one key at a time.
- **Portal-shaped gravity.** As Portal becomes the default onboarding path, features are likely to be designed and tested Portal-first. Nothing stops us using our own OpenRouter key, but the well-trodden path drifts away from our deployment shape.

## The risk that matters more, and it isn't commercialization

**Nous has no apparent intention of making Hermes multi-tenant, and multi-tenancy is our entire product.**

Three issues, all opened by the community, all sitting unresolved:

| Issue | Opened | Status |
|---|---|---|
| [#527](https://github.com/NousResearch/hermes-agent/issues/527) — Gateway permission tiers (Owner/Admin/User/Guest) | 2026-03-06 | P2, `needs-decision`, no maintainer response, unassigned |
| [#11430](https://github.com/NousResearch/hermes-agent/issues/11430) — Per-user memory isolation in group chats | 2026-04-17 | P2, unassigned |
| [#34352](https://github.com/NousResearch/hermes-agent/issues/34352) — Solving the multi-tenant Hermes problem | 2026-05-29 | `needs-decision`, unassigned; spans 12+ related issues |

`#527` has been open for nearly five months on a project shipping 180 commits a week. That is not a backlog problem. `needs-decision` with no maintainer response means the maintainers have not decided this is their product's job — which is coherent, because Hermes' identity is *"the agent that grows with you."* Singular *you*. Nous is building a personal agent that happens to be self-hostable. We are building a business platform.

**This risk is worse than the commercialization risk in every dimension that matters:**

- It is **already realized**, not speculative.
- It is **not mitigable by pinning**. A stale pin protects us from churn; it cannot add features that don't exist.
- It **doesn't resolve favorably with time**. If anything, Portal makes the personal-agent framing *more* central, since it monetizes individual subscribers.
- It sits **directly on our roadmap** — per-employee memory and role permissions are the two things Matt named as must-do-but-not-yet-done.

## Could we just upstream what we need?

Partially, and it's worth doing where cheap, but it doesn't solve the problem.

- **`line-group-mention.patch` is a genuinely good upstream PR.** LINE is the only major Hermes adapter without a `require_mention` switch — Discord, Slack, Telegram, Mattermost, WhatsApp, Matrix, Feishu, DingTalk and Photon all have one. This is a clear gap-fill with an obvious argument, and merging it deletes one of our patches. `patches/README.md` already identifies this.
- **`line-dm-pairing.patch` Phase 2 is plausible** (`patches/line-dm-pairing.tests.patch` exists specifically to accompany that PR). Phase 6c — the QR invite path — is deliberately not being submitted.
- **The multi-tenancy work is not upstreamable by us.** #34352 is an architectural change in `needs-decision`. We cannot force that decision, and carrying a fork of it would be a far larger burden than the two thin patches we maintain today.

So: upstreaming reduces the *patch tax*, which is our smallest problem. It does nothing for the *architecture mismatch*, which is our largest.

## Bottom line

Don't leave Hermes because Nous might monetize. The commercialization looks benign by 2026 standards — model resale and managed hosting alongside a genuinely MIT-licensed, genuinely thriving agent.

Leave — or, more precisely, stop deepening the dependency — because **Hermes is a personal agent and we are shipping a multi-tenant business product**, the gap between those is exactly where our next two features live, and upstream has declined to decide that the gap is theirs to close.

The commercialization worry and the architecture worry point the same direction, so the conclusion is robust either way. But it's worth being clear-eyed that the second one is the one carrying the weight — because it changes what we should build, not just what we should worry about.

---

### Sources

- [Nous Portal](https://portal.nousresearch.com/info) · [Cloud](https://portal.nousresearch.com/cloud) · [Hermes docs — Nous Portal integration](https://hermes-agent.nousresearch.com/docs/integrations/nous-portal)
- [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent) · [releases](https://github.com/NousResearch/hermes-agent/releases)
- Issues [#527](https://github.com/NousResearch/hermes-agent/issues/527), [#11430](https://github.com/NousResearch/hermes-agent/issues/11430), [#34352](https://github.com/NousResearch/hermes-agent/issues/34352), [#9514](https://github.com/NousResearch/hermes-agent/issues/9514), [#31988](https://github.com/NousResearch/hermes-agent/issues/31988)
- [Hermes Agent security docs](https://hermes-agent.nousresearch.com/docs/user-guide/security)
