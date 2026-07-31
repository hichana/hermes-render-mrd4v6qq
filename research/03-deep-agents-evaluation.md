# 03 — Deep Agents evaluation

Matt's impression was that LangChain/LangGraph means building an agent graph from the ground up, but that Deep Agents might be a different, more batteries-included harness — closer to Hermes.

**That impression is right about what Deep Agents is, and it still doesn't make it the answer.**

---

## What Deep Agents actually is

`deepagents` (pip/`uv add deepagents`, MIT, ~27k stars, ~3.1k forks, JS/TS port exists) is what LangChain calls an **agent harness** — one layer above the framework:

- **`langgraph`** — the runtime (durable execution, checkpointing, streaming, human-in-the-loop)
- **`langchain`** — the framework (`create_agent`, the core agent loop, middleware)
- **`deepagents`** — the harness (an opinionated agent that runs out of the box)

It shipped in March 2026 and hit 0.2 two months later, with LangChain publicly "doubling down." So it is real, current, and strategically backed — not an experiment.

**What's in the box:**

- **Planning** — `write_todos` via `TodoListMiddleware`, for decomposing multi-step tasks
- **Sub-agents** — a built-in `task` tool spawning ephemeral subagents with isolated context
- **Filesystem** — `ls`, `read_file`, `write_file`, `edit_file`, `delete`, `glob`, `grep`, `execute`, over pluggable backends (LangGraph State, LangGraph Store, local FS, sandboxes, custom)
- **Context management** — summarization, large-tool-result eviction, context offloading to files
- **Memory** — `AGENTS.md` files for persistent context; long-term memory via LangGraph Store
- **Skills** — progressive, on-demand domain knowledge (same idea as Hermes skills)
- **Human-in-the-loop** — `interrupt_on` for approval gates on sensitive tools
- **Tools** — plain functions, LangChain tools, MCP servers
- **Sandboxes** — Daytona, E2B, Modal backends for shell execution
- **Model-agnostic** — any tool-calling LLM; frontier APIs, Baseten/Fireworks, or self-hosted via Ollama/vLLM/llama.cpp

That is a genuinely impressive, genuinely batteries-included harness. Matt's read is accurate.

## Is it the right harness for us?

> **Revised 2026-07-31.** An earlier version of this section argued Deep Agents was "the wrong tool." That was too strong, and it was arguing against a position nobody held. Corrected below.

**What it does not supply, and we would build regardless.** From the docs there is no mention of chat platform integrations (no Slack, Telegram, or LINE), multi-user support, per-user permissions, authentication, or multi-tenant architecture. The permission system covers *filesystem* access control, not *user* access control. Nothing receives a LINE webhook, verifies a signature, resolves a reply token, distinguishes a DM from a group, gates on mentions, or decides whether a given human may talk to the agent at all.

This is **not an objection to Deep Agents** — it is true of every harness in its category, and adopting one on the explicit understanding that we write the gateway ourselves is a coherent, correct plan. Hermes is unusual in bundling a gateway; that bundling is precisely what forces us to accept its personal-agent identity model along with it. Unbundling is the point.

**What it does supply, and is genuinely worth having:** the tool-calling loop with retries and error handling, middleware (summarization, PII masking, tool selection, retry policy), a skills mechanism, human-in-the-loop interrupts, and **MCP client support** — which is the real answer to "we don't want to hand-write every integration." Both Hermes and the LangChain family have it. Neither ships a Google Calendar or CRM tool; both let you connect an MCP server that already does.

**The remaining question is narrow: `create_deep_agent` or `create_agent`?**

Planning, subagent delegation, virtual filesystem, context offloading, large-result eviction — the distinguishing features of Deep Agents — all serve long-horizon autonomous work, which we've said we don't need. LangChain's own guidance: start with `create_agent` for minimal setups, use `create_deep_agent` when you need planning, files, and delegation. On their decision rule we are a `create_agent` shop.

But this is a **mild preference, not an argument**, and it should not carry weight in the strategy decision:

- Unused capabilities in a library cost approximately nothing.
- The two share a foundation — `create_deep_agent` is `create_agent` plus middleware. Switching is close to a one-line change.
- The decision can be made last, empirically, against real conversations.

**So: Deep Agents is a legitimate way to get a harness without building one. It just doesn't decide anything.** The framework is the small, late, reversible part of this; the gateway is the large, early, irreversible part. That's the whole reason [`06`](06-recommendation.md) recommends sequencing them separately — not because Deep Agents is a poor choice.

## What we'd be signing up to build

To reach LINE parity with what's running today, on any LangChain-family stack:

| Piece | Reference in this repo | Notes |
|---|---|---|
| Webhook receiver + `X-Line-Signature` HMAC verification | upstream adapter | `line-bot-sdk` handles the primitives |
| DM vs group vs multi-person routing (`groupId` / `roomId` / `userId`) | upstream adapter | Straightforward, well-documented |
| Reply-token handling (single-use — order matters) | `line-group-mention.patch` | Our patch already places the gate ahead of the token stash for this reason |
| Mention gate + per-group mode store | **`modules/line/render_mention.py`** | **Already ours, already unit-tested. Carries over unchanged.** |
| DM pairing (codes, approval, rate limits) | `line-dm-pairing.patch` | Reimplement; behavior spec is in `SERVICES.md` |
| QR invite tokens | `line-dm-pairing.patch` + `skills/line-invite/` | Reimplement; the QR generator is already ours |
| Template Buttons / postback handling | `line-group-mention.patch` | |
| Session keying + history | Hermes session DB | LangGraph checkpointer, thread-per-(platform, chat, user) |

Meaningful work, but **not** a from-scratch effort: the mention gate is already a standalone tested module of ours, the invite skill is ours, and the pairing behavior is fully documented in `SERVICES.md` and `ARCHITECTURE.md`. We would be *reimplementing against our own written spec*, not reverse-engineering.

## The licensing boundary — and why it does not apply to us

> **Revised 2026-07-31.** The earlier version of this section overstated the licensing risk. `langgraph-api` is **optional**, and on the plan we're actually considering we never touch it. Corrected below.

Matt's instinct: *LangChain is also promoting their own services, but I'm less scared, since their offerings are directed at us (developers) rather than at end customers the way Nous' hosted platform is.*

**That instinct is correct, and the licensing boundary is avoidable by construction.** The tollbooth sits at a layer we would be replacing with our own service anyway.

| Component | License | Do we need it? |
|---|---|---|
| `langgraph` (library), `langchain-core`, model integrations | MIT | **Yes — free** |
| `deepagents` harness | MIT | **Yes — free** |
| `langgraph-checkpoint-postgres` | MIT | **Yes — free** |
| `langgraph-api` — pre-built server: HTTP, persistence, task queues, streaming | Elastic License 2.0 | **No.** Our FastAPI service *is* this layer |
| LangSmith Deployment / Agent Server / managed sandboxes | Proprietary | No |
| LangSmith self-hosting | Enterprise tier only | No |
| LangSmith tracing (optional, useful) | SaaS, free tier 5k traces/mo | Optional |

**The critical distinction: `langgraph-api` is a deployment product, not a dependency.** The MIT libraries are ordinary Python packages. You `uv add deepagents`, call `create_deep_agent(...)`, and invoke it in-process inside whatever web service you already have. Nothing phones home, nothing checks a key, nothing requires a LangChain account.

`langgraph-api` exists for teams who want a ready-made agent server rather than writing one. Since ngraph-gateway *is* our agent server — it has to be, because it owns the LINE webhook, identity, and permissions — we would simply never install it. LangChain describe this path themselves:

> Use the `langgraph` library to define your logic, but skip the official CLI. You'll need to build a custom FastAPI wrapper to handle persistence, the API layer, and streaming yourself.

That is the plan already, for independent reasons. **The licensing boundary is real, and it lands on a component we have no use for.**

One genuine observation survives from the earlier framing, downgraded from risk to context: when LangChain enumerate what production deep agents need — durable execution, memory, multi-tenancy with auth/authz/RBAC, HITL, observability, sandboxes, integrations, scheduled runs — they place multi-tenancy and RBAC on the managed side. Not as a licensing trap, but as a statement of fact about where that work lives: **no harness gives you multi-tenancy. You either buy a platform or you build it.** Hermes doesn't have it either ([#527](https://github.com/NousResearch/hermes-agent/issues/527), [#34352](https://github.com/NousResearch/hermes-agent/issues/34352)). That's the finding, and it is framework-independent.

Pricing, if we ever opt into tracing: LangSmith Developer free (5k traces/mo), Plus $39/user/mo, Enterprise custom. LangGraph Platform from ~$35/mo with production deployments cited at $200–500/mo — **not on our path**.

## The reframe

The comparison that's been on the table is **Hermes vs. Deep Agents**. Both are harnesses. Neither of them is where our problem lives.

What we actually need to build is:

1. a **multi-channel gateway** (LINE now, maybe Telegram/Slack later, plus our own UI)
2. a **per-user identity model** that survives across LINE *and* HTTP
3. **role-based tool and data permissions**
4. **per-employee memory** (Honcho)
5. an **agent loop** with business-system tools

Items 1–4 are ours to build on any stack. Item 5 — the only part a harness supplies — is the *smallest* and most replaceable piece, and for a knowledge-plus-integrations agent it's close to a commodity: `create_agent` with a tool list, or `deepagents`, or the Claude Agent SDK, or a hand-rolled tool loop.

**We have been shopping for a harness when what we need is a platform.** That's why neither Hermes nor Deep Agents fits: the harness is the part we should care least about, and it's the only part either product is selling.

The practical consequence is in [`06-recommendation.md`](06-recommendation.md): build 1–4 as ours, keep 5 swappable, and defer the framework question until it's a contained decision instead of a bet.

---

### Sources

- [Deep Agents overview](https://docs.langchain.com/oss/python/deepagents/overview) · [Sandboxes](https://docs.langchain.com/oss/python/deepagents/sandboxes) · [reference](https://reference.langchain.com/python/deepagents)
- [langchain-ai/deepagents](https://github.com/langchain-ai/deepagents)
- [The Runtime Behind Production Deep Agents](https://www.langchain.com/blog/runtime-behind-production-deep-agents)
- [Doubling down on Deep Agents](https://www.langchain.com/blog/doubling-down-on-deepagents)
- [Agent Middleware](https://www.langchain.com/blog/agent-middleware) · [Agents docs](https://docs.langchain.com/oss/python/langchain/agents)
- [LangGraph is MIT-Licensed, but Your Production Deployment Might Not Be](https://rvernica.github.io/2026/03/langchain-license)
- [Custom Authentication and Access Control for LangGraph](https://www.langchain.com/blog/custom-authentication-and-access-control-in-langgraph) · [custom_auth how-to](https://github.com/langchain-ai/langgraph/blob/main/docs/docs/how-tos/auth/custom_auth.md)
- [LangSmith pricing](https://pecollective.com/blog/langsmith-pricing/) · [LangGraph pricing guide](https://www.zenml.io/blog/langgraph-pricing)
- [LINE Messaging API — group chats](https://developers.line.biz/en/docs/messaging-api/group-chats/) · [line-bot-sdk-python](https://github.com/line/line-bot-sdk-python)
