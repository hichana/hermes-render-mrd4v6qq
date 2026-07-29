# LINE desktop mention fallback — Implementation Plan

> Lives at `plans/line-desktop-mention-fallback-plan.md` (admin-only, never packaged).
> Extends `plans/line-group-mention-mode-plan.md` — read that first for why the
> gate exists and why it's a thin patch + our own module.

---

## Status (2026-07-29)

Not started. Plan only.

---

## Context — the bug, confirmed by observation

Reported by Matt, 2026-07-29: in a LINE group chat containing the Hermes agent
and a human colleague, typing `@` on **iOS** pops the member picker listing
*both* the colleague and the agent. Doing the same in the **desktop** LINE app
lists only the colleague. The Official Account is absent from the picker, so
there is no way to select it, so the agent can't be addressed.

### Why that breaks the gate

`is_self_mentioned()` (`modules/line/render_mention.py:92`) keys entirely off
LINE's structured mention entity:

```python
if mentionee.get("type") == "all":
    return True
if mentionee.get("isSelf") is True:
    return True
```

That entity is constructed **client-side**, by the picker, at compose time. It
is what turns typed characters into a `mention` object carrying
`index`/`length`/`userId`/`isSelf`. No picker selection → no entity → the
webhook arrives with no `mention` key at all → `_mentionees()` returns `[]` →
`evaluate()` falls through to `GateDecision(allow=False, reason="no-mention")`
(line 471) and the message is dropped.

The desktop user *can* still type the characters `@Hermes`. They just arrive as
ordinary text that we currently throw away.

### Two candidate causes, and which one it is

Worth recording because they have different fixes and only one of them is ours:

| | What it would mean | Fix |
|---|---|---|
| **(a) Picker won't offer the bot** | Entity never constructed on desktop | Ours — stop requiring the entity |
| **(b) Picker offers it, entity lost in transit** | LINE platform defect | Theirs — report it; no code change would help |

Matt's direct observation of the picker's contents settles this as **(a)**.

### Not documented by LINE either way

Neither [group chats](https://developers.line.biz/en/docs/messaging-api/group-chats/)
nor [receiving messages](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)
mentions any client-platform restriction on mentioning an Official Account. So
this is inferred from observed behavior, not from a spec, and LINE could change
it in either direction without notice. That argues for a fallback that is
*additive* — if desktop starts emitting entities tomorrow, the entity path keeps
working and the fallback simply stops being exercised.

Filing feedback with LINE is reasonable but is **not** the path to a fix: it may
well be deliberate (desktop feature parity lags mobile broadly), and even if
they agree it's a defect we'd be waiting on a third party's client release cycle
for something affecting a live client instance.

---

## The fix

Accept a **second, textual signal** of a self-mention, consulted only when the
entity path finds nothing: a message whose text *begins with* `@<configured
name>`.

New optional env var:

```
LINE_MENTION_NAMES=ng,ngraph
```

Comma-separated bare tokens, no `@`, case-insensitive. **Unset or empty ⇒ the
fallback is entirely disabled and behavior is byte-for-byte identical to
today.** That is the safe-rollout property: shipping this changes nothing until
a client's `.env` opts in.

Resulting flow:

- **Mobile** — picker → entity present → matches on `isSelf`, exactly as now.
- **Desktop** — types `@ng` manually → no entity → matches on text.

Both reach the agent; the gate accepts either signal.

### Why this reverses a documented design decision, deliberately

The module docstring at line 95 currently says, in as many words:

> `isSelf` is LINE's own signal … **no regex, no display-name matching**, no
> comparison against our own user ID.

That was the right call when the entity was assumed universally available. It
isn't. This plan knowingly reintroduces display-name matching, so the docstring
must be **updated, not quietly contradicted** — with the reason (desktop picker)
recorded inline, or the next person to read it will "fix" the regression back
out.

Blast radius is contained by four constraints:

1. **Anchored at position 0 only.** Not mid-sentence. `@ng` in the middle of a
   sentence between two humans is not addressed to us.
2. **Only in `mention` mode.** `always` mode never consults it (it allows
   everything anyway); the entity path is untouched.
3. **Only against explicitly configured tokens.** Never auto-derived from the
   bot's LINE profile display name — those are multi-word and unparseable (you
   cannot tell where `@NGraph Assistant hello` stops being the name).
4. **Short distinctive tokens are the operator's job.** `@ng` beats the real
   display name: easier to type on a keyboard, fewer typos.

### The boundary rule — matters more than it looks, in Japanese

Naive `\b` after the token is **wrong for this deployment.** Python's `\b` is
defined against `\w`, which under Unicode includes kana and kanji. For a Fukui/
Ishikawa client base writing `@ngこんにちは` with no space — normal Japanese
input, since Japanese doesn't delimit words with spaces — `@ng\b` would *fail to
match* and the message would be dropped exactly as it is today. The bug would
look fixed in English testing and remain broken for the actual users.

Use a negative lookahead on **Latin word characters only**:

```python
rf"@{re.escape(name)}(?![A-Za-z0-9_])"
```

This allows Japanese (or punctuation, or end-of-string) immediately after the
token, while still preventing the token `ng` from matching `@ngraphics`.

Add an explicit Japanese test case. This is the single most likely thing to be
got wrong and shipped green.

### Detection and stripping must not drift apart

If detection matches but stripping doesn't, the agent receives a prompt starting
`@ng` **and `parse_mode_command()` stops recognising `@ng mode always`** — the
in-chat toggle silently breaks for desktop users only. Guarantee consistency by
having both paths call one span-returning function:

```python
def name_prefix_span(text: str) -> Optional[Tuple[int, int]]:
    """Span of a leading ``@<configured-name>``, or None."""
```

- `is_self_mentioned(message)` → entity check first, then
  `name_prefix_span(text) is not None`. (It already receives the whole
  `message`, so it can read `text` itself — no signature change.)
- `strip_self_mentions(text, message)` → existing entity spans **plus** the
  name-prefix span, fed through the same sort-and-splice at line 169.

### Env var read freshly, but a restart is still required

Follow `default_mode()`'s precedent (line 264) and read `os.environ` on every
call rather than caching at construction — it keeps the module internally
consistent and makes tests trivial to monkeypatch.

**This does not make it a live toggle.** Per `CLAUDE.md`, the process
environment is populated from `/opt/data/.env` at container start; reading it
fresh *within* the process changes nothing until the process restarts. So
`LINE_MENTION_NAMES` sits on the **restart** side of the ledger with
`LINE_REQUIRE_MENTION`, not the no-restart side with the mode store. Ship it via
`admin-tools/env-sync push <slug>`, and remember `hermes gateway restart` is a
silent no-op on this deployment.

---

## Phases (TDD — test first, per `CLAUDE.md` Mode 1)

### Phase 1 — `name_prefix_span` (pure, unit-tested)

Red, in `tests/test_render_mention.py`, new `NamePrefixTests`:

- unset / empty `LINE_MENTION_NAMES` → `None` (fallback disabled)
- `@ng hello` with `LINE_MENTION_NAMES=ng` → span `(0, 3)`
- case-insensitive: `@NG`, `@Ng`
- multiple configured tokens, each matches
- **`@ngこんにちは` matches** (the Japanese boundary case)
- `@ngraphics hello` with token `ng` → `None` (Latin lookahead holds)
- mid-sentence `hey @ng` → `None` (anchored)
- leading whitespace before `@` tolerated
- `@` alone, empty text, non-string input → `None`, no raise
- whitespace/empty entries in the env value ignored (`"ng, ,ngraph"`)

Green: implement, compiling the pattern per call from env.

### Phase 2 — wire into detection + stripping

Red:

- `is_self_mentioned` true for a text message with **no** `mention` key but a
  configured name prefix
- still true for entity mentions with no configured names (no regression)
- still true for `@all`
- `strip_self_mentions` removes the name prefix, collapsing whitespace
- entity span **and** name prefix in one message → both removed, no double-splice
- `@ng mode always` → strips to `mode always` → `parse_mode_command` returns
  `always` **(the toggle-breaks-on-desktop regression guard)**

Green: implement both call-outs.

### Phase 3 — gate-level behavior

Red, in the existing gate tests:

- `mention` mode, text-prefix message → `allow=True`, `reason="mentioned"`
- …and it **opens the follow-up window** for that user (`evaluate` already calls
  `window.record` at line 460, so the desktop user pays the `@ng` cost only on
  the first message of a conversation — assert it, it's a real UX property)
- `always` mode unaffected
- DMs unaffected
- fallback disabled ⇒ every existing gate test still passes unchanged

### Phase 4 — config surface + docs

- `patches/line-group-mention.patch`: add `LINE_MENTION_NAMES` to
  `plugin.yaml`'s `optional_env`, alongside `LINE_REQUIRE_MENTION`. **Patch
  line counts shift** — re-verify it applies (`docker build`).
- `modules/line/render_mention.py` module docstring: update the "no
  display-name matching" claim (see above) and document the fallback under
  *Behavior*.
- `CLAUDE.md`: in the group-response-mode paragraph, note `LINE_MENTION_NAMES`
  is an env var and therefore restart-gated, unlike the mode store.
- `README.md`: env var table entry, with the desktop-picker rationale — an
  operator seeing this var needs to know why it exists or they won't set it.
- `PACKAGING.md`: **no change** — no new files, `render_mention.py` already has
  its `COPY` (Dockerfile:49).

### Phase 5 — validation

- `python3 -m pytest tests/ -q` (74 existing + new; all green)
- `./scripts/smoke-test.sh` — the existing assertions at smoke-test.sh:123-140
  already cover the module being present and importable inside the container;
  extend line 126's in-container import assertion to also touch
  `name_prefix_span` so a botched `COPY` or syntax error surfaces at build time.
- Lint / format / type-check per repo config.

### Phase 6 — live verification (needs explicit go-ahead)

Touches a client-facing instance, so same gate as the parent plan's Phase 4.

1. Add `LINE_MENTION_NAMES` to `clients/<slug>.env`; `hermes-env-sync push <slug>`.
2. Confirm the tool reports **restart-verified**, then independently confirm via
   `/opt/data/gateway.pid` (both `pid` *and* `start_time` must differ).
3. From **desktop**, send `@ng ping` → agent replies.
4. From desktop, send an unrelated message within 90s → agent replies
   (follow-up window).
5. From **mobile**, mention via the picker → still works (no regression).
6. Two humans chatting with no `@ng` → agent stays silent (no false positives).
7. From desktop, `@ng mode always` → mode card appears, mode changes.

---

## Risks

| Risk | Mitigation |
|---|---|
| **Japanese boundary bug ships green** — English tests pass, real users still broken | Explicit `@ngこんにちは` test in Phase 1; Latin-only lookahead, never `\b` |
| **Detection/stripping drift** — desktop users lose the in-chat mode toggle | Single shared `name_prefix_span`; explicit Phase 2 regression test |
| False positive on ordinary chat | Anchored at position 0; `mention` mode only; operator-chosen distinctive tokens |
| Operator sets a too-generic token (`a`, `hi`) | README guidance to pick something distinctive; note it's matched case-insensitively at message start |
| Patch line drift breaks the build | Expected and *desired* — fails loudly at `docker build`, per `patches/README.md` |
| Someone "fixes" the regex back out as a violation of the docstring | Docstring updated in Phase 4 with the reason recorded inline |

## Non-goals

- **Making the mention render as a highlighted chip.** It won't — a typed `@ng`
  is plain grey text in the LINE UI. Purely cosmetic; functionally identical.
  Nothing we can do about it from the server side.
- Auto-deriving tokens from the bot's profile display name (see constraint 3).
- Any change to DM handling, the pairing store, or `always` mode.
- Chasing LINE for a desktop-client fix as a prerequisite — orthogonal, and this
  fix stays correct whichever way that goes.
