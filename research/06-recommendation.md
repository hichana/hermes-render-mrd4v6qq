# 06 — Recommendation

---

> **Revised 2026-07-31** after two clarifications: (a) the planned UI is a per-business **admin console**, not a chat UI, so `plans/api-server-identity-plan.md` is off the critical path; (b) the Deep Agents licensing concern was overstated — `langgraph-api` is optional and we'd never install it. The recommendation is unchanged; the reasoning behind it is stronger. Details in [`04`](04-options-matrix.md) R4 and [`03`](03-deep-agents-evaluation.md).

## The call

**Don't migrate to Deep Agents. Don't stay on the current trajectory either.**

**Own the gateway. Rent the agent.** Build `ngraph-gateway` — LINE + identity + roles + Honcho — as our own service, and let it drive Hermes as a stateless completion engine to start with. Defer the agent-framework decision until Phase 2 has answered it with running code.

## Why, in five steps

**1. The stated problems aren't the deciding ones — and the remaining one is narrower than two drafts of this document claimed.** The patch tax is our *most* bounded problem: half a day per bump against tooling that works. The admin console (R4) turns out to be feasible on Hermes' management REST API — see [`04`](04-options-matrix.md). What is left is **R3: role differentiation inside an agent.** Hermes can express *which* agents an employee reaches (one profile per virtual employee, own LINE channel, own allowlist). It cannot express *what* two employees of the same agent may each do — authorization is binary, and [#527](https://github.com/NousResearch/hermes-agent/issues/527) has sat in `needs-decision` since 2026-03-06.

**This materially weakens the case for a near-term rebuild, and the plan below is deliberately structured so that's fine** — Phase 0 is valuable on every path, and the decision point sits after it, not before.

**2. Hermes is a personal agent; we ship a multi-tenant business product.** Every gap maps to an upstream issue in `needs-decision` with no maintainer response — [#527](https://github.com/NousResearch/hermes-agent/issues/527) has been open five months on a project shipping 180 commits a week. That's not a backlog, it's a scope boundary. The Nous commercialization worry is reasonable but secondary; this is the risk that's already realized and doesn't improve with time.

**3. Deep Agents is a fine harness and a non-answer to this question.** It supplies the agent loop, middleware, skills, HITL and MCP; it supplies no gateway, no multi-user model, no auth — but that's true of every harness in its class, and adopting one on the understanding that we write the gateway is exactly right. Its licensing is a non-issue for us (`langgraph-api` is optional; we'd never install it). The point is that **choosing it doesn't answer anything we're stuck on**, because everything we're stuck on lives in the layer no harness provides.

**4. We've been shopping for a harness when we need a platform.** Multi-channel gateway, per-user identity, role permissions, per-employee memory, business tools. The agent loop — the only piece a harness sells — is the smallest and most replaceable part of that list, and for knowledge-plus-integrations work it's near-commodity.

**5. So separate the two decisions we've welded together.** Gateway ownership is not negotiable and not framework-dependent: it carries identity, permissions, and the client relationship. Agent framework *is* negotiable, and it's cheap to defer behind one interface. Once our layer owns identity, Hermes is no longer asked to know who the user is — and R2/R3/R4, all identity questions, stop depending on Hermes' answers.

## What to do, in order

**This week — no bets required:**

1. Build the **Honcho integration on the live instance** (`plans/hermes-plan.md` Phase 3). Ships R2 to the client now, portable to every option. *Now the top item — see the reordering note below.*
2. Run the unverified **Phase 0 experiments** — `session_search` cross-employee scoping, and whether the built-in memory really goes quiet (#45422/#18404). These are open questions about a live client-facing instance and shouldn't wait on a strategy discussion.
3. **Write down the access-control model** the admin console needs: employee, role, agent, grant. One page. It's the schema for the gateway, the spec for the console, and it will show immediately how far it is from what Hermes stores.
4. Submit **`line-group-mention.patch`** upstream. Deletes a patch if it merges; tells us about upstream responsiveness if it doesn't.

> **Reordering note (2026-07-31).** `plans/api-server-identity-plan.md` was item 1 in the first draft, on the assumption the UI was a chat surface. It's an admin console, so that experiment no longer gates anything on the critical path. Keep the plan — it becomes live again the day we want employees chatting with an agent through our own UI instead of LINE — but don't spend the week on it.

**Next 4–6 weeks:** Phase 1 (shadow gateway) and Phase 2 (cut LINE over, Hermes as engine). Details and exit criteria in [`05-rebuild-sketch.md`](05-rebuild-sketch.md).

**Then decide** on the framework, at the decision point, on evidence.

## The honest objection

**If Phase 2 works, it will have shown we didn't need Hermes** — and by then we'll have reimplemented the gateway, meaning we did most of the rebuild while telling ourselves we weren't rebuilding.

That's true, and it's the strongest argument against this plan. Two reasons it's still right:

- **The gateway work is required on every path.** Roles and custom-UI identity cannot be built inside Hermes today. Even the stay-on-Hermes option needs this layer. It's not migration cost; it's the cost of the next two features.
- **It converts a bet into an experiment.** A big-bang rebuild is 6–9 weeks of no client value wagered on research — including research about a framework we'd be adopting for capabilities we don't use. The phased path delivers R2 in week one, R3 by week five, keeps the live client on rails, and answers the framework question from production behavior instead of from blog posts.

The counter-argument for pure Option A also deserves stating plainly: **with 1–3 clients, everything works today, and the patch tax is bounded and well-tooled.** If R4 comes back green — if the API server does yield clean per-user identity — then Option A gets substantially stronger and the timeline stretches out. That's precisely why the API-server experiment is item 1.

## What would change this recommendation

| Finding | Effect |
|---|---|
| The admin console turns out to be acceptable as a thin wrapper over `admin-tools/env-sync` + SSH JSON edits | R4 survives on Hermes and the timeline stretches. Worth a one-day spike before committing to Phase 1 — this is now the biggest single swing factor, replacing the API-server experiment. |
| The UI later grows an end-user chat surface after all | `plans/api-server-identity-plan.md` becomes a blocker again. Run it then. |
| Nous accepts [#34352](https://github.com/NousResearch/hermes-agent/issues/34352) or [#527](https://github.com/NousResearch/hermes-agent/issues/527) and starts shipping multi-tenancy | The category mismatch closes. Stay, contribute, drop this. |
| Client count jumps to 10+ or the SaaS pivot lands | Accelerate. Config conventions don't survive that scale; go straight to owning the platform. |
| Requirements shift to genuinely autonomous long-horizon work | Re-open Deep Agents seriously — that's the workload it's built for, and this analysis wouldn't hold. |
| Two clients need Slack/Telegram before the custom UI | Hermes' 20+ adapters get much more valuable. Delay Phase 1; reconsider. |
| Phase 2 shows unacceptable latency/cost through Hermes' API server | Skip Phase 4's deliberation and go to `create_agent` sooner. |

## One thing to be wary of

Framework anxiety is a real tax of its own. We're one live client into a business whose value is bespoke AI integrations for Fukui businesses — the differentiated part is understanding those businesses and wiring the agent into their actual systems, not which harness runs the loop. R7 (business-system integrations) is the requirement that makes clients pay, and it is the one requirement where **every option in the matrix scores identically**.

The recommendation above is chosen partly because it lets integration work continue uninterrupted the entire time. If following it ever means integrations stall for a month, that's the signal it's gone wrong.
