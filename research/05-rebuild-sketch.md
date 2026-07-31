# 05 — What a rebuild actually costs

Matt asked for a real cost/benefit on rebuilding, not a hand-wave. This is the concrete version: the architecture, the effort, what we'd lose, and the phased path that gets most of the benefit before most of the cost.

---

## Target architecture

The same shape whether the agent ends up being Hermes-behind-an-API (Option D) or `create_agent` (Option C). That's the point — the agent is the swappable part.

```
                    LINE Platform                      Browser (custom UI)
                          │                                    │
                    webhook POST                          HTTPS + session
                          │                                    │
                          ▼                                    ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │                    ngraph-gateway  (our FastAPI service)             │
   │                                                                      │
   │  1. Channel adapters   LINE: signature verify, DM/group, reply token,│
   │                        postback.  UI: session auth.                  │
   │                        → both emit the same InboundMessage           │
   │                                                                      │
   │  2. Identity resolution   (channel, channel_user_id) → EmployeeId    │
   │                           the single place identity is decided       │
   │                                                                      │
   │  3. Authorization         EmployeeId → Role → allowed tools + data   │
   │                           enforced here, before dispatch             │
   │                                                                      │
   │  4. Conversation policy   mention gating, group mode, pairing,       │
   │                           invites  ← modules/line/render_mention.py  │
   │                                                                      │
   │  5. Context assembly      Honcho peer context for THIS employee      │
   │                                                                      │
   │  6. Agent dispatch        ─────────── the swappable seam ─────────►  │
   └──────────────────────────────────────────────────────────────────────┘
                    │                                          │
                    ▼                                          ▼
        Postgres  (employees, roles,          ┌────────────────────────────┐
        pairings, invites, group modes,       │  Agent  (one of:)          │
        threads, audit log)                   │   · Hermes /v1/chat/…      │
                    │                         │   · langchain create_agent │
                    ▼                         │   · anything else          │
              Honcho (peers, sessions,        └────────────────────────────┘
              per-employee memory)                        │
                                                    tools / MCP
                                                          ▼
                                             calendar · CRM · docs · search
```

**The load-bearing idea:** identity, authorization, and memory are decided **before** the agent is called, in code we own, in one place. The agent receives an already-authorized request with already-scoped context. It never needs to know who the user is — which is exactly the thing Hermes gets wrong and every harness leaves to you anyway.

Everything currently living on a Render volume as JSON (`line-approved.json`, `line-pending.json`, `invites.json`, `modes.json`) becomes Postgres rows: queryable, backed up, transactional, and visible without SSH. That alone retires a meaningful chunk of `SERVICES.md`.

## Effort estimate

One engineer, focused. Ranges assume Python/FastAPI, `line-bot-sdk`, Postgres, and reuse of what we already have.

| Component | Est. | Notes |
|---|---|---|
| **LINE channel adapter** | 5–8 d | Webhook, HMAC signature verify, DM/group/multi-person routing, reply tokens, postback. `line-bot-sdk` supplies the primitives. |
| **Conversation policy** | 2–3 d | **`modules/line/render_mention.py` ports over largely as-is, with its tests.** Pairing/invite behavior is fully specified in `SERVICES.md` + `ARCHITECTURE.md` — reimplementing from our own written spec, not reverse-engineering. |
| **Identity + roles + Postgres schema** | 4–6 d | The genuinely new capability. Employees, roles, channel-identity mapping, tool allowlists per role, audit log, migrations. |
| **Honcho integration** | 3–4 d | Peer per employee, session mapping, context assembly, token budget. Would be built anyway on any option. |
| **Agent dispatch + tools** | 4–6 d | Thin seam, then business-system tools (the actual product value). Cheaper against Hermes' API first; more when swapped to `create_agent` with streaming and tool loop. |
| **Custom UI backend** | 5–8 d | Auth, thread list, message history, SSE streaming. Not the frontend. |
| **Ops** | 3–5 d | Render service + managed Postgres, migrations, structured logging, health checks, smoke test in the spirit of `scripts/smoke-test.sh`. |
| **Migration + cutover** | 3–5 d | Import approvals/invites/modes from the volume JSON, dual-run, cut LINE webhook URL over, keep rollback ready. |
| **Total** | **~29–45 days** ≈ **6–9 weeks** | |

Halve the front half if we take the phased path below and stop after Phase 2.

For comparison, the do-nothing path isn't free: ~½ day per Hermes bump, plus R3 (roles) and R4 (custom UI identity) still unbuilt — and R4 currently has no known solution on Hermes at all.

## What we'd lose

Worth being blunt, because Matt specifically valued this.

**Real losses:**

- **"Full-featured and actively improved, and clients get the improvements."** This is the strongest argument for staying and it should not be dismissed. But per [`01`](01-current-stack-assessment.md), the improvements reaching our clients today are the LINE adapter and the session store; the rest is disabled, unused, or operator-only. What we'd give up is mostly *optionality* — the ability to switch on a Hermes capability later without building it.
- **The dashboard.** API-key UI, file browser, TUI chat. Genuinely convenient for us. Replacement is a small internal admin page, or nothing — `admin-tools/env-sync` already covers the config path deliberately.
- **20+ platform adapters.** Telegram, Slack, Discord, WhatsApp, Signal, email — free today, each ~3–5 days to build ourselves. Real cost if a client wants Slack. (Also: adapters we don't build are attack surface we don't defend.)
- **Free upstream features.** Cron, sessions, skills, self-improvement — available if we ever want them.
- **Sunk tooling.** `upgrade-preflight.sh`, the manifest coverage test, the Hermes-shaped parts of `smoke-test.sh`, `UPGRADING.md`, `HISTORICAL-GOTCHAS.md`. Retired, not transferred. That said: this is a *reduction* in maintained surface, and the principles behind them (assert state not logs, closed-world manifests, verified restarts) transfer to whatever we build.
- **Speed for client #2.** Provisioning a second business on Hermes today is a checklist. On a half-built platform it's a project.

**Not actually losses:**

- Per-business container isolation — unchanged, it's a Render property.
- `admin-tools/env-sync` — framework-agnostic.
- `modules/line/render_mention.py` — becomes a first-class module instead of a `COPY` into someone else's package.
- Honcho — stack-agnostic.
- `plans/hermes-plan.md` — it *is* the requirements spec for the new platform.
- The operational discipline in `ARCHITECTURE.md` / `HISTORICAL-GOTCHAS.md`.

## Phased path

Ordered so each phase is independently valuable and independently abandonable. **The decision point is at the end of Phase 2, on evidence.**

### Phase 0 — Do now, regardless (1 week)

Nothing here is a bet on the outcome.

1. **Build the Honcho integration on Hermes**, per `plans/hermes-plan.md` Phase 3 — `userPeerAliases`, one workspace per business, `pinUserPeer: false`. Delivers R2 to the live client now and is portable to every option.
2. **Run the Phase 0 experiments** that plan already flags as unverified — especially `session_search` cross-employee scoping and whether the built-in memory really goes quiet (#45422/#18404). These are unknowns about a *live client-facing instance* and they should not stay unknown while a strategy discussion runs.
3. **Run `plans/api-server-identity-plan.md`.** It's currently the single highest-information experiment available: it either unblocks the custom UI on Hermes or confirms the biggest reason to move. Half a day, and it decides a large part of this.
4. **Submit `line-group-mention.patch` upstream.** LINE is the only major Hermes adapter without `require_mention`. If it merges, one patch is deleted; if it stalls, that's a useful signal about upstream responsiveness to our needs.

### Phase 1 — Gateway skeleton, shadow mode (2–3 weeks)

Build `ngraph-gateway` — LINE adapter, identity resolution, roles, Postgres, conversation policy — and run it in **shadow**: a second LINE channel, or a staging Official Account, with the production webhook untouched. Import the existing approvals/invites/modes as a one-way seed. Dispatch to Hermes' OpenAI-compatible API.

Live client sees nothing. Rollback is deleting a service.

**Exit criteria:** a test employee DMs the shadow channel and gets a correct, role-appropriate, Honcho-contexted reply; a group message without a mention is correctly ignored; with a mention, answered.

### Phase 2 — Cut over LINE, keep Hermes as the engine (1–2 weeks)

Point the production LINE webhook at our gateway. Hermes stays, unpatched and pinned, purely as a completion engine behind its API server. **R3 (roles) ships here.** Rollback is repointing one webhook URL.

Note the side effect: with the gateway owning identity, the two LINE patches stop being load-bearing on the deployed path, and `UPGRADING.md`'s cost drops sharply — we're no longer patching a moving target.

### ⟨ Decision point ⟩

By now we will know, from running code rather than from research: how much of Hermes' behavior we actually relied on, whether latency and cost through the API server are acceptable, and how much work the remaining swap is. **Decide here whether to continue, not now.**

### Phase 3 — Custom UI backend (2 weeks)

Add the UI's API to the gateway: auth, threads, history, SSE. Identity flows through the layer that already resolves it, so R4 is solved by construction rather than by discovering what Hermes does with an asserted user ID.

### Phase 4 — Swap the agent, if warranted (1–2 weeks)

Replace the Hermes dispatch with `create_agent` + our tools. Contained by design: one seam, one implementation, feature-flagged per client, A/B-able against the Hermes path with real conversations. If Hermes is still winning at this point, don't do it — that's a finding, not a failure.

## Why phased beats big-bang here

With 1–3 clients, a big-bang rebuild is 6–9 weeks of zero client value against a system that works, betting on research. The phased path front-loads the two capabilities we're missing (R2 in week 1, R3 by week ~5), keeps the live client on rails throughout, makes every step revertible, and converts the framework question from a bet into an experiment with an answer.

It also fixes the thing that's really wrong with the current setup, which isn't Hermes: **we don't own the layer where identity, permissions, and the client relationship live.** That layer should be ours on every possible future — including the one where we keep Hermes forever.
