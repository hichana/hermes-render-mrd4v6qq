# API server user identity — Verification Plan

> Lives at `plans/api-server-identity-plan.md` (admin-only, never packaged).
> Blocks any client-facing UI we build against Hermes' OpenAI-compatible API
> server. Referenced from `plans/hermes-plan.md` Phase 0 and Phase 8.

---

## Status (2026-07-30)

**Unresolved.** Desk research only — nothing has been run against a real Honcho
workspace. Do not design a customer dashboard on assumptions from this document
until the experiment below has actually been performed.

---

## Why this matters

Relevant the moment we build our own UI (a client-facing dashboard, an internal
console) that reaches the agent through `/v1/chat/completions` instead of through
a chat platform. The two doors into the agent do not carry identity the same way:

| | Chat platform (LINE, Slack, …) | API server (`/v1/chat/completions`) |
|---|---|---|
| Who the user is | Derived from the platform's signed payload (e.g. LINE's HMAC-verified webhook → real user ID) | **Asserted by the caller** |
| Session scoping | Gateway keys it: `agent:main:<platform>:dm:<chat_id>` | `X-Hermes-Session-Key` header, or it collapses |
| Honcho peer | `userPeerAliases` maps the platform user ID → a distinct peer | **Unknown — see below** |
| Trust boundary | Hermes verifies the platform's signature | Hermes trusts our app completely |

The platform column is the model `plans/hermes-plan.md` Phase 3 is built on:
`userPeerAliases` maps each human's *platform runtime ID* to their own Honcho
peer, which is what stops one person's facts being modeled onto everyone. The API
column has no platform runtime ID to map, so that whole mechanism may simply not
engage.

## What the source says

By default the API server stamps requests with a single shared channel
(`"chat_id": "api"`) — there is no per-user concept at all. To scope memory per
user you must send `X-Hermes-Session-Key`, documented in
`gateway/platforms/api_server.py` as *"a stable per-channel identifier that
scopes long-term memory (e.g. Honcho sessions) across transcripts."*

It requires `API_SERVER_KEY`, and the source is explicit about why: accepting a
caller-supplied memory scope without authentication would let a client *"inject
itself into another user's long-term memory scope by guessing a key."* (For where
`API_SERVER_KEY` comes from in this template, see ARCHITECTURE.md § "Where the
`HERMES_GATEWAY_TOKEN` fits" — the Blueprint generates the value; the API server
is off by default.)

## The open question

`userPeerAliases` maps platform runtime IDs to Honcho peers, and the API path has
no platform user ID. So whether a session key yields a distinct Honcho **peer**,
or merely a distinct **session** under one blended peer, is unverified.

That distinction is the whole point: per-user *transcripts* keep conversations
tidy, per-user *peers* keep the agent's model of one person from being applied to
another. The latter is exactly the blending failure Honcho's peer model exists to
prevent, and the same failure mode as the built-in `USER.md` problem called out
in `plans/hermes-plan.md` Phase 3.

## Experiment to resolve it

Run on a throwaway instance, never against a live client instance.

- [ ] Point a test instance at a real Honcho workspace (a scratch workspace, not
      any client's) and confirm Honcho is actually the live memory layer.
- [ ] Enable the API server on that instance: `API_SERVER_ENABLED=true` plus a
      real `API_SERVER_KEY`.
- [ ] Drive two separate conversations through `/v1/chat/completions` with two
      different `X-Hermes-Session-Key` values, each stating a distinct,
      unmistakable fact ("my dog is named Kuro" / "my dog is named Shiro").
- [ ] Inspect the Honcho workspace: does it record **one peer or two**?
- [ ] Cross-check behaviorally — in conversation A, ask something that would
      surface B's fact. Leakage means one blended peer regardless of what the
      workspace listing suggests.
- [ ] Repeat with `X-Hermes-Session-Key` omitted entirely, to confirm the
      documented collapse to `"chat_id": "api"` is what actually happens.
- [ ] Record the answer here, then fold the resolved behavior into
      `plans/hermes-plan.md` Phase 3 and delete this plan's "unresolved" framing.

## Consequence either way

Our app becomes the sole guarantor of user isolation on that path, because the
session key is caller-supplied and Hermes cannot verify it. That is a fair trade
— it is also what lets us implement the per-user RBAC Hermes doesn't have (see
`plans/hermes-plan.md` Phase 4, the open Owner/Admin/User/Guest gap) — but it has
to be a deliberate design decision, not something we discover after shipping.

Concretely, whichever way the experiment lands:

- Our UI must derive the session key from *its own* authenticated session, never
  from anything the browser can set.
- `API_SERVER_KEY` is then a shared secret between our backend and the instance,
  and must never reach client-side code.
- If the answer is "one blended peer," a client-facing multi-user dashboard on
  this path is off the table until upstream grows a per-user peer concept for the
  API server — one instance per user, or route through a platform adapter
  instead.
