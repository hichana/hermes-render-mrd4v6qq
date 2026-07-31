# research/ — Agent platform strategy

**Date:** 2026-07-31
**Question asked:** Should NGraph keep building client agents on Hermes Agent, or move to something like LangChain's Deep Agents?
**Status:** Research complete. Recommendation in [`06-recommendation.md`](06-recommendation.md).

---

## Contents

| File | What's in it |
|---|---|
| [`01-current-stack-assessment.md`](01-current-stack-assessment.md) | What we actually depend on in Hermes, the real coupling surface, and the upgrade tax measured from this repo |
| [`02-nous-platform-risk.md`](02-nous-platform-risk.md) | Is the Nous OSS-deprioritization worry justified? Evidence, and the risk that turns out to matter more |
| [`03-deep-agents-evaluation.md`](03-deep-agents-evaluation.md) | What Deep Agents actually is, what it doesn't do, and the LangChain licensing boundary |
| [`04-options-matrix.md`](04-options-matrix.md) | Five real options scored against our four requirements |
| [`05-rebuild-sketch.md`](05-rebuild-sketch.md) | What a rebuild concretely costs, what we'd lose, phased plan |
| [`06-recommendation.md`](06-recommendation.md) | The call, the reasoning, and the triggers that would change it |

## Inputs that shaped the analysis

Answered by Matt, 2026-07-31:

- **Capabilities needed:** knowledge + integrations only. No terminal, no code execution, no long-horizon autonomous work.
- **Scale, next 6–12 months:** 1–3 businesses.
- **Custom UI:** coming.
- **Decision being made:** genuinely evaluating a rebuild.

---

## Executive summary

**1. The three pain points are one problem, and it isn't a Hermes bug.**
Per-employee memory, role-based restriction, and the patch tax are all symptoms of the same category mismatch: Hermes is a *personal* agent harness — one agent, one owner, one memory — and we are retrofitting it into a *multi-tenant business* platform. Every gap maps to an open upstream issue sitting in `needs-decision`: [#527 gateway RBAC](https://github.com/NousResearch/hermes-agent/issues/527), [#11430 per-user memory isolation](https://github.com/NousResearch/hermes-agent/issues/11430), [#34352 multi-tenancy](https://github.com/NousResearch/hermes-agent/issues/34352). None is assigned. We aren't waiting on a roadmap; we're waiting on a decision that runs against the product's identity.

**2. Deep Agents is a legitimate harness choice — it just doesn't decide anything.** *(revised 2026-07-31)*
Adopting it on the explicit understanding that we write the gateway ourselves is coherent and correct. It supplies the agent loop, middleware, skills, HITL and MCP support; it supplies no gateway, no multi-user model, no auth — but neither does any harness in its category, and Hermes' bundling of a gateway is exactly what forces its personal-agent identity model on us. The only open sub-question is `create_deep_agent` vs `create_agent` (LangChain's own rule points at the latter for our workload), and that is close to a one-line change made last, on evidence. **Its licensing risk was overstated in the first draft: `langgraph-api` is an optional deployment product, not a dependency — building our own service means we never install it.** See [`03`](03-deep-agents-evaluation.md).

**3. The stack underneath it is the right comparison.**
What we actually need is a multi-channel gateway + per-user identity + role permissions + business-system tools + a custom UI. On LangGraph those are first-class primitives (Store namespaces, custom auth handlers, thread-per-user). On Hermes they are config conventions applied against the grain, and `plans/hermes-plan.md` is the evidence — it is a long, careful document about not letting the defaults leak one employee's data to another.

**4. The custom UI is an *admin console*, not a chat UI — and that makes the case stronger, not weaker.** *(revised 2026-07-31)*
The first draft assumed client employees would chat with the agent over HTTP, which made `plans/api-server-identity-plan.md` the blocker. **That is not the plan.** The UI is a per-business admin console for managing agents and controlling which employees can reach which agent. So it never touches `/v1/chat/completions`, and the API-server identity question drops off the critical path.

**Corrected same day — a first pass at this claimed the console would need SSH and hand-edited volume JSON. Wrong.** Hermes' dashboard exposes a documented management REST API — `/api/pairing` (list/approve/revoke), `/api/env`, `/api/config`, `/api/messaging/platforms`, `/api/sessions/*`, all scopable with `?profile=<name>`. A console is a **proxy-and-reconcile job, not a wall**, and R4 on Hermes is 🟡 not 🔴. Full endpoint table and the three residual frictions — the verified-no-op `POST /api/gateway/restart`, the two-store union that defines DM access, and the fact that the same API also serves provider keys and a PTY so it must be proxied and never exposed — are in [`04`](04-options-matrix.md) under "R4 clarified."

**What this leaves as the real gap is R3, not R4:** *which agents* an employee may reach is expressible today (one profile per virtual employee, each with its own LINE channel and allowlist). *What an employee may do within an agent they can reach* is not — that's [#527](https://github.com/NousResearch/hermes-agent/issues/527), still `needs-decision`. Which of those two the console actually needs is the open question, and it is now the highest-value thing to pin down.

**5. Honcho is portable — invest in it either way.**
It is a standalone service with its own API, SDK, MCP server, Postgres storage, self-hosting path, and an existing LangGraph integration. Neither Hermes nor Deep Agents owns it. It is the one piece of the target architecture we can build *now*, on Hermes, and carry across unchanged. Do not defer it pending this decision.

**6. No harness gives you multi-tenancy — you buy a platform or you build it.** *(revised 2026-07-31)*
Worth stating in its framework-independent form. LangChain place multi-tenancy and RBAC on the managed LangSmith side; Nous have left [#527](https://github.com/NousResearch/hermes-agent/issues/527) and [#34352](https://github.com/NousResearch/hermes-agent/issues/34352) in `needs-decision`. Neither is withholding it — it's simply not what a harness is. So the multi-tenant layer is ours on every path, and the only real question is whether we write it deliberately or keep approximating it with per-instance config conventions.

**7. Recommendation: split the decision that's currently welded together.**
Gateway ownership and agent-framework choice are two decisions, and we're treating them as one. The gateway is what we must own — it carries identity, permissions, and the client relationship. The agent framework is what we can defer.

So: **don't migrate the running client, and don't rebuild the agent yet.** Build our own LINE gateway + identity/permission service, and have it drive Hermes as a stateless completion engine behind its OpenAI-compatible API. Once our gateway owns identity and memory, Hermes' multi-tenancy gaps stop being our problem — we're no longer asking it to know who the user is. Then the agent-framework swap becomes a contained, reversible change we can make on evidence rather than on worry.

The honest tension: if that step works, it will have demonstrated we didn't need Hermes. That's a real outcome, not a flaw in the plan — see [`06`](06-recommendation.md).

---

## Sources

- [Deep Agents overview — LangChain docs](https://docs.langchain.com/oss/python/deepagents/overview)
- [langchain-ai/deepagents](https://github.com/langchain-ai/deepagents)
- [The Runtime Behind Production Deep Agents](https://www.langchain.com/blog/runtime-behind-production-deep-agents)
- [Doubling down on Deep Agents](https://www.langchain.com/blog/doubling-down-on-deepagents)
- [LangGraph is MIT-Licensed, but Your Production Deployment Might Not Be](https://rvernica.github.io/2026/03/langchain-license)
- [Hermes Agent — Honcho Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/honcho)
- [Honcho — Hermes integration guide](https://honcho.dev/docs/v3/guides/integrations/hermes)
- [Nous Portal](https://portal.nousresearch.com/info)
- Hermes issues [#527](https://github.com/NousResearch/hermes-agent/issues/527), [#11430](https://github.com/NousResearch/hermes-agent/issues/11430), [#34352](https://github.com/NousResearch/hermes-agent/issues/34352), [#9514](https://github.com/NousResearch/hermes-agent/issues/9514)
- [LINE Messaging API — group chats](https://developers.line.biz/en/docs/messaging-api/group-chats/), [receiving messages](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)
