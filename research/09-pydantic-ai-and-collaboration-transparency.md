# 09 — Pydantic AI, and the collaboration-transparency requirement

**Date:** 2026-08-02
**Trigger:** Matt raised two new arguments for rebuilding, independent of [`06`](06-recommendation.md)'s scoring: (1) patch-based development gives him "no solid way to co-develop with Claude — hoping it works" rather than both of them reading the same code; (2) Hermes needs real guardrail work and is "a modification of their core use case." He proposed archiving this repo into a subfolder and building fresh at root on Pydantic AI + probably FastAPI.

**Short answer: the new arguments are real, but they don't overturn [`06`](06-recommendation.md) — they re-derive it from a different angle, and they answer the one question `06` deliberately left open (which framework for Phase 4). They also correct the proposed *mechanic*: this is not a subfolder move.**

---

## 1. Decomposing "no solid way to co-develop"

Before scoring anything, it's worth checking *where* the patch-opacity problem actually lives, because the answer changes what fixes it.

All three patches touch exactly two files:

```
plugins/platforms/line/adapter.py
plugins/platforms/line/plugin.yaml
```

(789 + 451 + 86 lines across `line-multi-channel`, `line-dm-pairing`, `line-group-mention`, plus matching test patches against `tests/gateway/test_line_plugin.py`.) That's the entire coupling surface named in [`01`](01-current-stack-assessment.md) and reconfirmed by `scripts/upgrade-preflight.sh`'s manifest. **Nothing we patch touches Hermes' agent loop, tool-calling, or LLM dispatch.** The opacity Matt is describing is 100% in the LINE gateway/adapter layer — webhook handling, pairing, mention gating, multiplexing — not in "the agent."

That matters because [`06`](06-recommendation.md)'s Option D already replaces exactly that layer, in Phase 1, as the *first* thing built — before any framework decision. Once `ngraph-gateway` owns LINE, the three patch files stop existing at all: that logic becomes ordinary Python in a repo we own outright, no `git apply`, no context-matching against a tag that moves 180 commits/week. Matt and Claude read and edit it the same way either of us reads and edits anything else in this repo.

**So: call this requirement R8 — collaboration transparency, the business logic must live in code both of us can read and edit directly, not mediated through patches against a vendored tree — and R8 is not new. It's R1 (LINE gateway) seen from a co-development angle, and Option D was already going to satisfy it in Phase 1, independent of whatever happens to the agent.**

The one place R8 would *not* be satisfied by Option D alone: if we keep calling Hermes' `/v1/chat/completions` for completions (Phase 2's default), the agent-loop code itself is still Hermes', still opaque to us — just no longer patched, only called over HTTP. Whether that residual opacity matters depends on whether we ever need to read or modify *that* code, which is exactly the Phase 4 question `06` already deferred. R8 sharpens the case for eventually resolving Phase 4, not for skipping straight there.

## 2. The guardrails argument is R3, restated

"Hermes needs a lot of work to build guardrails, it's a modification of their core use case" is [`04`](04-options-matrix.md)'s R3 finding, word for word: *"Hermes is a personal agent; we ship a multi-tenant business product... [#527](https://github.com/NousResearch/hermes-agent/issues/527) has sat in `needs-decision` since 2026-03-06."* R3 was already scored as the requirement that actually kills Option A (see [`04`](04-options-matrix.md), "Reading the matrix"). This isn't new evidence changing the recommendation — it's independent confirmation of the strongest reason the recommendation already gives for not staying on Option A long-term.

## 3. Pydantic AI as the Phase 4 candidate

`03` evaluated Deep Agents as a harness candidate and concluded the choice barely matters — the gateway is the large decision, the framework is the small, late, reversible one. Pydantic AI gets the same treatment here, since it's a new candidate for that same slot (the "Agent dispatch" seam in `05`'s architecture diagram — `Hermes /v1/chat/…`, `langchain create_agent`, or `anything else`).

**What it is:** `pydantic-ai` (pip/`uv add pydantic-ai`, MIT, actively developed by the Pydantic team — v1.0 shipped, currently at v2.0). Explicitly built with "FastAPI-like" developer experience as the design goal. Model-agnostic (OpenAI, Anthropic, Gemini, others). Structured/validated outputs and tool schemas are the core value proposition, not an add-on — every tool call and structured output is a Pydantic model, validated before it reaches application code.

**What it supplies, mapped against what we said we don't need:** no planning middleware, no built-in sub-agent delegation, no virtual filesystem, no sandboxes — the Deep Agents feature set we already said didn't apply to us. What it does supply: the tool-calling loop, dependency injection for passing request-scoped context (identity, role, tenant) into tool calls, streaming, MCP client support, durable-execution/retry patterns, and — the part that matters for R3 — a first-party middleware package (`pydantic-ai-middleware`, before/after hooks on the agent lifecycle) built specifically for adding guardrails, logging, and security checks without forking anything. That last point is a closer fit for R3 than either Hermes (binary approvals, no per-role enforcement) or Deep Agents (filesystem permissions, not user permissions) supplied.

**Licensing:** cleaner than the LangChain family. Pydantic AI's core library is MIT with no equivalent of `langgraph-api`'s Elastic License 2.0 boundary — there's no separate "hosted agent server" product whose license we'd need to route around by construction the way `03` had to for Deep Agents. Pydantic's commercial product (Logfire, observability) is optional and analogous to LangSmith, not a gate on the library.

**One structural point worth flagging that didn't apply to the Deep Agents comparison:** because Pydantic AI is a plain in-process Python library and `ngraph-gateway` would be FastAPI regardless of which agent framework sits behind it, choosing Pydantic AI means the agent can run **inside** `ngraph-gateway` as one service, rather than `05`'s sketch of an HTTP call out to a separately-deployed Hermes container. That's not unique to Pydantic AI — `create_agent` would collapse the same way — but it's a genuine simplification over the Option-D-with-Hermes-as-engine default, and it means Phase 4 (if and when it happens) could plausibly merge into Phase 1 rather than waiting for a separate cutover, since there'd be no second deployment to stand up.

## 4. Updated recommendation

**R8 and the sharpened R3 do not move the call away from Option D. They remove ambiguity from two of its open questions:**

- They confirm Phase 1 (own the gateway) is worth doing on its own terms even before touching the framework question — it directly fixes the co-development pain Matt is describing today, and it's the same Phase 1 `06` already prioritized for R3/R4.
- They name Pydantic AI as the leading candidate to resolve Phase 4 with, when that phase's decision point arrives — ahead of Hermes-as-engine and on par with or ahead of `create_agent`, specifically because of the middleware/guardrails fit (R3) and the option to skip a second deployed service.

**What changes concretely from `06`'s plan:** none of the phase ordering or effort estimates in [`05`](05-rebuild-sketch.md). What changes is confidence, not sequencing — Phase 4 is no longer an open "decide later between three unequal options," it's "decide later, and Pydantic AI is the front-runner unless Phase 1–2 turns up a reason otherwise."

**Correction to the proposed mechanic.** Archiving this repo into a subfolder and building at root isn't quite the right shape for what Phase 1 actually is. This repo is the Hermes *deployment* — it keeps `ngraph-main` (a live client) running throughout Phases 0–2, unpatched and pinned, as the completion engine. `ngraph-gateway` is a **new, separate service** (new repo or a new top-level directory with its own deploy target — either works, it's a small choice) that talks to this one over HTTP during the transition and can be torn down independently if it doesn't work out. Collapsing them into one root now would mean giving up the cheap-rollback property that's the entire reason `05` phases this the way it does. Worth keeping `modules/line/render_mention.py` in mind as a literal file to carry over unchanged, per `05`'s "What we'd lose" table — it already ports as-is.

## What would change this

Same triggers as [`06`](06-recommendation.md)'s table, plus: if Phase 1 turns up guardrail or context-assembly needs that Pydantic AI's middleware doesn't cleanly cover, that's evidence for `create_agent` or a hand-rolled loop instead — exactly the kind of finding Phase 4 exists to surface, and a cheap one to act on since nothing upstream of that seam depends on the choice.

---

### Sources

- [Pydantic AI docs](https://ai.pydantic.dev/) · Context7 `/pydantic/pydantic-ai` (v1.0–v2.0, benchmarked high-reputation, 3000+ snippets)
- `pydantic-ai-middleware` — before/after lifecycle hooks for guardrails/logging (Context7 `/vstorm-co/pydantic-ai-middleware`)
- This repo's `patches/*.patch` (file-level diff confirms LINE-adapter-only coupling surface, 2026-08-02)
- Carried forward: all sources in [`03`](03-deep-agents-evaluation.md) and [`04`](04-options-matrix.md)
