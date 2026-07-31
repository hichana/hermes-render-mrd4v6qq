# 04 — Options matrix

Five real options, scored against what we actually need.

---

## The requirements

Derived from Matt's answers and from `plans/hermes-plan.md`:

| # | Requirement | Why it's on the list |
|---|---|---|
| **R1** | **LINE gateway** — DM + group, mention gating, pairing/allowlist, invites | The live product. Non-negotiable. |
| **R2** | **Per-employee memory** — employee A's context never reaches employee B | Named by Matt as must-do. Honcho is the intended mechanism. |
| **R3** | **Role-based permissions** — capability differs by role, enforced not prompted | Named by Matt as must-do. |
| **R4** | **Per-business admin console** — client manages their agents and controls which employees can reach which agent | Confirmed as coming. *Revised 2026-07-31: this is an admin UI, not a chat UI — see note below.* |
| **R5** | **Per-business tenancy isolation** | Already solved by one Render service per business; must not regress. |
| **R6** | **Low upgrade/maintenance tax** | The stated irritant. |
| **R7** | **Business-system integrations** (calendar, CRM, docs) | The actual product value for a knowledge+integrations agent. |

Explicitly *not* requirements: code execution, terminal, long-horizon autonomy, sub-agent delegation, self-improvement.

> **R4 clarified, 2026-07-31.** The planned UI is a **per-business admin console**, not an end-user chat surface. Employees keep talking to agents through LINE. The console is where a client's manager sees their agents and grants or revokes employee access. The console never calls `/v1/chat/completions`, so `plans/api-server-identity-plan.md` is not a blocker for it.
>
> **Correction, same day — an earlier revision of this note claimed a console would require SSH-driven `.env` upserts and volume JSON edits. That was wrong.** Hermes' dashboard exposes a documented management REST API that covers most of what a console needs, including the pairing store I claimed was SSH-only:
>
> | Need | Endpoint | Works? |
> |---|---|---|
> | List pending + approved messaging users | `GET /api/pairing` | ✅ |
> | Approve / revoke a platform user | `POST /api/pairing/approve` · `/revoke` | ✅ live, no restart |
> | Read/write env vars | `GET`/`PUT`/`DELETE /api/env` | ✅ write, ⚠️ see restart caveat |
> | Read/write `config.yaml` | `GET`/`PUT /api/config` | ✅ |
> | Per-agent scoping | `?profile=<name>` on the management families | ✅ |
> | Channel config + connection test | `GET`/`PUT /api/messaging/platforms/{id}` | ✅ |
> | Sessions, logs, usage/cost analytics | `/api/sessions/*`, `/api/logs`, `/api/analytics/usage` | ✅ |
> | Create/list profiles | — | ❌ no documented REST family |
> | Invite tokens (`invites.json`) | — | ❌ our patch's store, no upstream API |
>
> **So an admin console over Hermes is feasible.** What survives as friction, in descending order of seriousness:
>
> 1. **`POST /api/gateway/restart` is a verified silent no-op on this deployment** (`ARCHITECTURE.md`, confirmed live 2026-07-27) — it shells out to `hermes gateway restart`, which has no registered service because the gateway runs as the container's bare main process. So the one class of change that needs a restart — env vars like `LINE_ALLOWED_USERS` — has a working *write* API and a broken *apply* API. `admin-tools/env-sync`'s USR1 + pid/start_time verification is the fix, and a console would have to call something that does that.
> 2. **Two stores, two reload semantics, one question.** DM access is the *union* of `LINE_ALLOWED_USERS` (env, restart-gated) and `line-approved.json` (pairing API, live per message). A console must reconcile both to answer "who has access?" — a data-model problem the API doesn't solve.
> 3. **The API is an admin superset and must be proxied, never exposed.** It also serves `/api/env` (provider keys in the clear), `/api/ops/*`, and a `/api/pty` WebSocket. Auth is a single shared basic-auth credential with no per-admin identity or roles, so our backend holds that credential and does its own RBAC in front. That's a normal, workable pattern — it just means the console still needs a backend of ours.
>
> Net: **R4 moves from 🔴 to 🟡 on Hermes.** It is a proxy-and-reconcile job, not a wall.

## The options

**A — Stay on Hermes, deepen it.** Finish `plans/hermes-plan.md`: Honcho with `userPeerAliases`, per-profile virtual employees, terminal disabled, keep patching.

**B — Rebuild on Deep Agents.** New service, `deepagents` harness, write our own LINE gateway.

**C — Rebuild on LangChain `create_agent` + our own service.** Same, but the minimal harness LangChain themselves recommend for our shape, inside our own FastAPI app on the MIT stack.

**D — Own the gateway, keep Hermes as the engine.** Build our LINE gateway + identity + permissions + Honcho as our service; call Hermes' OpenAI-compatible API for completions. Framework-agnostic by construction.

**E — Switch harness to OpenClaw.** Gateway-first design, multi-channel binding, agent teams with isolated workspaces, MIT, very large community.

## Scoring

Legend: ✅ solved · 🟡 workable with effort · 🔴 gap or fights the design · ⬜ we build it

| | **A** Hermes | **B** Deep Agents | **C** `create_agent` | **D** Our gateway + Hermes engine | **E** OpenClaw |
|---|---|---|---|---|---|
| **R1** LINE gateway | ✅ shipping (2 patches) | 🔴 none — build it | 🔴 none — build it | ⬜ build it, ours | 🟡 gateway-first, LINE support to verify |
| **R2** Per-employee memory | 🟡 Honcho + aliases; built-in memory off-switch is buggy (#45422/#18404); global memory store (#11430) | ⬜ Store namespaces, ours to define | ⬜ same, cleanly | ✅ our layer owns it; Hermes never sees identity | 🟡 isolated workspaces per agent, not per end user |
| **R3** Role permissions | 🔴 binary auth, #527 `needs-decision` 5 months | ⬜ filesystem perms only; user RBAC is ours | ⬜ ours, straightforward | ⬜ ours, enforced before dispatch | 🟡 control-plane framing helps; still ours |
| **R4** Admin console | 🟡 **feasible via `/api/pairing`, `/api/env`, `/api/config` + `?profile=`**; needs our proxy for RBAC, a working restart path, and reconciliation of two access stores | ⬜ ours (Postgres CRUD) | ⬜ ours (Postgres CRUD) | ✅ our layer already owns the state the console edits | 🟡 unknown, likely similar |
| **R5** Per-business isolation | ✅ container per business | ✅ same | ✅ same | ✅ unchanged | ✅ same |
| **R6** Maintenance tax | 🔴 ~½ day/bump + ~1.4k lines of tooling + 925 patch lines against 180 commits/wk | 🟡 no patches; LangChain churn + ELv2 runtime boundary | ✅ smallest surface, MIT only | 🟡 our gateway + a pinned Hermes we stop patching | 🔴 new upstream, new tax, less mature for our shape |
| **R7** Integrations | ✅ MCP + skills | ✅ MCP + tools | ✅ MCP + tools | ✅ either side | ✅ ClawHub + MCP |
| **Time to first value** | ✅ zero — it's live | 🔴 6–8 wks | 🔴 6–8 wks | 🟡 3–4 wks | 🔴 6–8 wks + learning |
| **Risk to live client** | ✅ none | 🔴 high | 🔴 high | 🟡 low — additive, revertible | 🔴 high |

## Reading the matrix

**Option A's failure is concentrated, not diffuse.** Hermes wins R1, R5, R7 outright and is workable on R2. It fails on R3 and R4. Notably, R6 (the stated irritant) is Hermes' second-*best* column after the things it does well; the patch tax is annoying but it is not what breaks this option. **The requirements that kill Option A are the two Matt hasn't built yet, not the one that's bothering him today.**

R3 and R4 are also the *same* requirement seen from two angles: both are "who is allowed to do what," one enforced at message time and one edited through a console. Hermes has no answer to either, because it has no first-class notion of an employee — only allowlists, pairing files, and profiles. Any option that solves R3 solves most of R4 for free, and vice versa. That is why they collapse into one piece of work in [`05`](05-rebuild-sketch.md) rather than two.

**B and C are the same option with different harnesses**, and C dominates B for our workload: same LINE gap, same everything-we-build-anyway, smaller surface, no unused planning/filesystem/sandbox machinery, and it stays entirely on the MIT stack. Choosing B over C would be choosing a set of features we've explicitly said we don't need. If a rebuild happens, it should be C.

**E is interesting and I'd stop short of recommending it.** OpenClaw is gateway-first — a control plane binding agents to channels — which is architecturally much closer to our shape than Hermes is, and Hermes' own #34352 cites OpenClaw's single-daemon-multi-agent-isolated-workspace design as the model to copy. It's MIT and enormous (topped 250k stars by March 2026, passing React). But: its isolation unit is the *agent*, not the *end user*, which is a different axis from what R2/R3 need; it's described as heavier and more local-machine-oriented while Hermes is the more VPS-and-cron-friendly of the two; and adopting it would trade a known upstream tax for an unknown one. Same category of product, same category mismatch. **Worth watching, not worth switching to.**

**D is the only option that doesn't force the framework choice.** It scores well on the requirements that kill Option A, at roughly half the time-to-value of a rebuild, additively, with the live client untouched and every step revertible. The reason it works is subtle and worth stating plainly:

> Once our own layer owns identity, memory, and permissions, Hermes stops being asked to know who the user is. Every one of R2/R3/R4 is a question about *identity*, and Hermes' answers to those questions are the thing that's broken. Take the question away and the breakage stops mattering. Hermes degrades to a completion engine with tools — a role it performs fine.

The cost is honest and should be named: **D reduces Hermes to something a plain `create_agent` call also does.** If D works, it will have proven that Option C was viable and that the harness was never the load-bearing part. That is a legitimate outcome — it converts an expensive, speculative, all-at-once bet into a cheap experiment that answers the question with evidence. But nobody should adopt D imagining it's a permanent home for Hermes. It's a way to find out.

## Cross-cutting: Honcho is the same in every column

Honcho is a standalone service — its own API and SDK, Postgres storage, an MCP server, a documented self-hosting path, a first-party Hermes integration, and an existing LangGraph integration. Its primitive is the **peer** (a human, an agent, a project), with many-to-many sessions, so "what does the agent know about employee A" is modeled natively rather than bolted on. Reported at the top of the memory leaderboards as of May 2026.

**Nothing about the Honcho investment is at risk in any of these options.** That makes it the correct next thing to build regardless of how this decision lands, and it should not be blocked on the decision.

One caveat carried from `plans/hermes-plan.md` Phase 3: on Hermes, Honcho *layers on top of* the built-in memory rather than replacing it, and the documented off-switch (`memory_enabled: false`, `user_profile_enabled: false`) is reported buggy — the memory prompt may still be injected. **Under Option D that caveat evaporates**, because our layer supplies the context and Hermes' own memory becomes irrelevant to what the agent is told about the user.

---

### Sources

- [Honcho + Hermes integration](https://honcho.dev/docs/v3/guides/integrations/hermes) · [Hermes Honcho docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/honcho) · [plastic-labs/honcho](https://deepwiki.com/plastic-labs/honcho/9.1-agent-framework-integrations)
- [OpenClaw vs Hermes Agent — Composio](https://composio.dev/content/openclaw-vs-hermes-agent) · [OpenClaw (Wikipedia)](https://en.wikipedia.org/wiki/OpenClaw) · [The New Stack](https://thenewstack.io/openclaw-hermes-agent-harness/)
- Hermes issues [#527](https://github.com/NousResearch/hermes-agent/issues/527), [#11430](https://github.com/NousResearch/hermes-agent/issues/11430), [#34352](https://github.com/NousResearch/hermes-agent/issues/34352)
