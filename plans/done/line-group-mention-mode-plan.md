# LINE group mention-mode — Implementation Plan

> Lives at `plans/line-group-mention-mode-plan.md` (gitignored, admin-only).

---

## Status (2026-07-28)

Built and locally verified: `modules/line/render_mention.py` (74 unit tests,
`python3 -m pytest tests/`), `patches/line-group-mention.patch` (86 lines),
Dockerfile wiring, two new `scripts/smoke-test.sh` assertions — full smoke test
green, including the patched adapter importing inside the real container.

Not done: live verification in a real LINE group (Phase 4), which needs an
explicit go-ahead since it touches a client-facing instance.

---

## Context

Today the LINE adapter replies to **every** message it can see, in DMs and in
group chats alike. Correct for a 1:1 client chat, wrong the moment a second
human is in the room — the agent interjects on messages the humans are
exchanging with each other.

We want:

- **DM (`source.type == "user"`): always reply immediately.** Current behavior,
  must not change — it's the whole client-facing product.
- **Group / multi-person room: reply only when @-mentioned**, with a toggle
  that turns the gate off so the agent responds to everything.

LINE is the only major adapter in this build without this gate (Discord, Slack,
Telegram, Mattermost, WhatsApp, Matrix, Feishu, DingTalk, Photon all ship
`require_mention`).

---

## Can this be done as middleware instead of a patch? — investigated, no

Checked on the live instance, 2026-07-28. Three candidate extension points
exist; none can gate an inbound platform message.

| Mechanism | What it actually is | Why it can't do this |
|---|---|---|
| **Event hooks** (`gateway/hooks.py`, `~/.hermes/hooks/<name>/HOOK.yaml` + `handler.py`) | Real, documented, no patch needed | Fires `agent:start` via `emit()`, which **discards handler return values** — "Errors in hooks are caught and logged but never block the main pipeline" (hooks.py:19). The deny-capable variant `emit_collect` is wired at exactly one call site, run.py:9520, for `command:<name>` slash commands only — the code comment above it says outright *"Plain chat is unaffected — only slash commands gate."* Separately, the `agent:start` context carries only `platform/user_id/chat_id/chat_type/session_id/message` (truncated to 500 chars) — **no raw webhook payload, so no `mention.mentionees[]` at all.** Even as an observer it can't see what we need. |
| **Middleware** (`hermes_cli/middleware.py`) | Genuinely named "middleware", plugins can register it | `VALID_MIDDLEWARE` is `{tool_request, tool_execution, llm_request, llm_execution}` (middleware.py:30-35) — all four are **agent-loop internals**. They intercept tool calls and LLM provider requests. Every one of them fires *after* the agent has already been invoked, which is precisely the thing we need to prevent. |
| **Plugin platform override** (`gateway/platform_registry.py` + `discover_plugins()`) | Viable — see below | Works, but has a worse failure mode than a patch. Evaluated properly rather than dismissed. |

### The plugin-override route, and why it's not the pick

It genuinely works: `discover_plugins()` runs in the gateway (run.py:6796),
scans bundled `plugins/platforms/` and `$HERMES_HOME/plugins/`
(hermes_cli/plugins.py:1341-1350), and `_create_adapter` checks the registry
**before** the built-in if/elif chain (run.py:8534-8539). So a plugin
registering `PlatformEntry(name="line", …)` with a `LineAdapter` subclass would
shadow the built-in adapter, no patch involved.

Rejected because:

- **It fails open, silently.** The subclass overrides private methods
  (`_handle_message_event`, `_handle_postback_event`). If upstream renames one
  or routes text through a different path, our override simply stops being
  called — and the symptom is *the bot answering every message in a client's
  group chat*, discovered in production. A patch fails **loudly at build time**
  ("patch does not apply", `patches/README.md`). `CLAUDE.md`'s Pattern 4 exists
  because of exactly this class of silent-success bug.
- **Shadowing a built-in platform name is undocumented usage.** The registry's
  own examples are new platforms (`irc`, `viber`). If upstream adds a guard
  against plugins shadowing built-ins, we fail open again.
- Plugins are opt-in (`_get_enabled_plugins()` — "None = opt-in default
  (nothing enabled)"), so it needs `config.yaml` mutation at boot anyway. The
  no-config-changes selling point doesn't survive contact.

### What we do instead: thin patch + our own module

This captures most of the maintenance win the middleware idea was reaching for.

`/opt/hermes/plugins/platforms/line/` is a real package directory on the
editable install — verified: `import plugins.platforms.line.adapter` resolves
to `/opt/hermes/plugins/platforms/line/adapter.py`. So **a new sibling module
dropped in that directory by a plain `COPY` is importable as
`plugins.platforms.line.render_mention`, with no patch touching it.**

Split accordingly:

- **All the substance** — mention detection, mention stripping, the mode store,
  the follow-up window, the Template Buttons payload, command parsing, the
  group-message orchestration — lives in `modules/line/render_mention.py` in
  this repo, `COPY`'d into that package dir. Ordinary Python we own outright.
- **The patch shrinks to call-outs**: one import, one attribute init, one `if`
  in `_handle_message_event`, one `if` in `_handle_postback_event`. ~20 lines
  against upstream instead of ~400.

Why this beats both alternatives: upstream drift can only break the thin hunk,
and it breaks it **loudly at `docker build`**. The bulk of the code is unit
testable in this repo with plain `python3 -m pytest` — **no upstream clone
required**, unlike `patches/line-dm-pairing.tests.patch`. And the thin patch is
still the artifact to offer Nous as a PR.

---

## Reference — confirmed internals (live instance, 2026-07-28)

Post-patch line numbers (image `v2026.7.7.2` + `patches/line-dm-pairing.patch`). Still accurate under `v2026.7.20`: upstream's only `adapter.py` change in that bump was a 2-line edit at ~L275, so anything below L275 shifts by +2 and anything above is unmoved.

- **`_resolve_chat` — adapter.py:412.** Already returns `(chat_id, chat_type)`
  with `chat_type` ∈ `dm|group|room`. No new source parsing needed.
- **`_handle_message_event` — adapter.py:1264.** resolve chat → **stash
  `replyToken`** → media download / text extraction → typing indicator (DM
  only) → `build_source` → `MessageEvent` → `handle_message`. Sets
  `user_name=user_id`, so group speakers surface as raw `U…` IDs.
- **`build_postback_button_message` — adapter.py:766.** Template Buttons with
  `data = json.dumps({"action": "show_response", "request_id": rid})`. Its
  docstring records the decisive UI fact: *"Template Buttons stay tappable from
  chat history, unlike Quick Reply chips which are dismissed the moment any new
  message arrives in the chat."*
- **`_handle_postback_event` — adapter.py:1328.** `json.loads(data)`, silent
  `return` on non-JSON, then branches on the parsed dict — a naturally
  backwards-compatible place to add a second `action`.
- **No mention handling anywhere** (`grep -i mention` → zero hits).

### LINE platform facts (confirmed in docs)

- Group text webhooks carry `message.mention.mentionees[]` with `index`,
  `length`, `type`, and **`isSelf: true`** when the mentionee is this bot. No
  regex, no display-name matching.
- `index`/`length` let us slice the `@BotName` out precisely.
- **`mention` exists only on text messages** — stickers/images/voice never
  carry one.
- Quick Reply: works in groups, but chips vanish on the next message → not a
  control surface. Rich menu: per-user or account-default only, **no per-chat
  rich menu exists**, and invisible on LINE for PC → cannot represent per-group
  state. ("QuickTap" isn't a Messaging API feature name.)

---

## Decisions

| Question | Decision |
|---|---|
| DM behavior | Unchanged — always reply. |
| `room` (multi-person) | Identical to `group`. |
| Default for an unseen group | **Mention-only.** A bot dropped into an active group must never spam it. `LINE_REQUIRE_MENTION` overrides the default; the store overrides both. |
| Follow-ups | **Window scoped to the last mentioner** (90s default). After the bot replies to A's mention, A's next messages — text *or* media — pass without an `@`; other members still need one. This is also the non-text answer: a sticker/photo passes iff it lands in that sender's open window. |
| Mention stripping | Slice `isSelf` mentionees only; mentions of other humans stay in the text. |
| Toggle state | **JSON store, re-read every message** (`GroupModeStore`) — mirrors `PairingStore.is_approved()`, so a flip takes effect immediately with no restart. |
| Toggle UI | **Template Buttons bubble** (chosen), plus a text command as the always-available floor. Buttons set an **absolute** mode, never "toggle" — stale bubbles stay tappable forever, and a relative flip from a stale bubble is wrong half the time. |
| Who may flip it | Anyone who passed the allowlist gate for that group. Every change logged with the setting user's ID. |

---

## Phase 1 — Module (ours) + tests, TDD — **DONE** (2026-07-28)

- [x] `tests/test_render_mention.py` — recreate the repo's `tests/` dir, house
      style from the removed `tests/test_patch_config.py` (`importlib` loader +
      `unittest.TestCase`, run under `python3 -m pytest`).
- [x] `modules/line/render_mention.py` — pure/semantic functions plus two small
      stateful classes. **Must not import from `adapter.py`** (the adapter
      imports it, not the reverse); the orchestrator takes the adapter as a
      parameter and only touches its public `send()`.
      - `is_self_mentioned(message) -> bool`
      - `strip_self_mentions(text, message) -> str`
      - `parse_mode_command(text) -> "mention"|"always"|"show"|None`
      - `GroupModeStore` — `get_mode(chat_id)` / `set_mode(chat_id, mode, by)`;
        atomic write (`mkstemp` + `os.replace` + `chmod 0600`) copied from
        `LineInviteStore`; **`get_mode` reads from disk every call, no memo**.
      - `FollowupWindow` — in-memory `{chat_id: (user_id, expiry)}`.
        Deliberately not persisted: a 90s window is *correct* to lose on
        restart.
      - `build_mode_buttons_message(chat_id, current_mode)`
      - `parse_mode_postback(data) -> mode|None`
      - `handle_group_message(adapter, …) -> bool` (True = handled/drop)

Test cases (write first, watch them fail):

- [x] **Detection**: `isSelf: true` → mentioned; only non-self mentionees →
      not; `type: "all"` → counts as mentioned; no `mention` key → not;
      malformed `mention` (not a dict, `mentionees` not a list) → not, no raise.
- [x] **Stripping**: leading mention → stripped + whitespace collapsed;
      mid-sentence → stripped in place; self + human mentionee → only self
      removed; **emoji before the mention** (LINE's `index` may not agree with
      Python code-point indexing — assert explicitly, fall back to substring
      removal if they disagree, do not ship the slice untested); out-of-range
      `index`/`length` → text unchanged, no raise.
- [x] **Store**: absent file → mention-only; absent file + `LINE_REQUIRE_MENTION`
      → follows env; round-trip; file mode 0600; corrupt JSON → default, no
      raise; **a second instance sees the first's write without reconstruction**
      (proves no caching — the whole point).
- [x] **Window**: mentioner passes inside N seconds; fails after; a different
      member inside the window fails; sticker/image from the mentioner passes
      inside, fails outside.
- [x] **Commands/postback**: `mode`/`mode always`/`mode mention` parse; unknown
      text → None; postback sets absolute mode; tapping a stale bubble twice is
      idempotent.

## Phase 2 — Thin patch — **DONE** (2026-07-28)

- [x] `patches/line-group-mention.patch`, generated against a clone with
      `line-dm-pairing.patch` already applied (both touch adjacent code).
      Contents, and nothing more:
      - `from plugins.platforms.line import render_mention` (top of adapter)
      - `self._mention = render_mention.MentionGate()` in `__init__`
      - `_handle_message_event`: a gate block placed **after** `_resolve_chat`
        and **before** the `replyToken` stash (:1270) — reply tokens are
        single-use with a short TTL, so don't bank one from a message we drop
      - `_handle_postback_event`: a `set_line_mode` branch before the existing
        `show_response` handling; unknown actions keep falling through
      - `plugin.yaml`: `LINE_REQUIRE_MENTION`, `LINE_MENTION_FOLLOWUP_SECONDS`
- [x] `Dockerfile`: `COPY modules/line/render_mention.py
      /opt/hermes/plugins/platforms/line/render_mention.py`, plus the second
      `COPY` + `git apply` **after** the existing one in the same `RUN`.
- [x] `patches/README.md`: new patch + the ordering constraint + a note that
      the logic lives in an unpatched sibling module.

## Phase 3 — Build + smoke test — **DONE** (2026-07-28)

- [x] Both `git apply` steps succeed.
- [x] Add assertions to `scripts/smoke-test.sh` in house style (numbered `# N.`
      comment, `docker exec` against the **live artifact**, `|| fail "…"`):
      (a) `render_mention.py` present in the image at the package path,
      (b) it actually *imports* under the Hermes venv —
      `docker exec … /opt/hermes/.venv/bin/python3 -c "import
      plugins.platforms.line.render_mention"` — which is the thing that would
      silently break if the package layout ever changed,
      (c) the adapter contains the patch marker.
- [x] `./scripts/smoke-test.sh` green.

## Phase 4 — Live verification — **NOT STARTED, needs explicit go-ahead**

Nothing has been deployed. The image builds and boots locally; no change has
touched `srv-d97k2t57vvec73ccpg2g`.

- [ ] Test group: Matt + bot + one other account. **Prerequisite:** "Allow
      joining group chats and multi-person chats" enabled in the LINE Official
      Account Manager, or the OA can't be added to a group at all.
- [ ] Group ID → `LINE_ALLOWED_GROUPS` in `clients/ngraph-main.env`, then
      `hermes-env-sync push ngraph-main` (this one *is* an env var, so it
      genuinely needs the verified restart). Confirm both `pid` and
      `start_time` changed in `/opt/data/gateway.pid`.
- [ ] Un-mentioned chatter → no reply, and a drop line in `logs/gateway.log`.
      That log line is ground truth; absence of a reply is not.
- [ ] `@bot <question>` → replies, logged prompt shows the mention stripped.
- [ ] Same person within 90s without `@` → replies; after 90s → silent; other
      person inside the window → silent; photo from mentioner inside → handled.
- [ ] Tap *Always reply* → JSON store changes on disk, un-mentioned chatter now
      answered, **with no restart**. Tap *Only when mentioned* → gated again.
- [ ] **DM regression check**: plain DM still replies immediately.

## Phase 5 — Docs

- [ ] `CLAUDE.md`: the mode store is a *third* state mechanism and sits on the
      **no-restart** side — say so next to the existing env-vs-pairing warning,
      since that section exists precisely to stop someone assuming an edit took
      effect when it didn't.
- [ ] `SERVICES.md`: add the mode-store path to the SSH-inspectable state list.
- [ ] `patches/README.md` (Phase 2).
- [ ] *(Deferred at Matt's request: `PACKAGING.md`, `ARCHITECTURE.md`.)*

---

## Follow-on work (out of scope)

- **No admin tooling for JSON state.** `admin-tools/env-sync` is `.env`-only.
  Inspecting/repairing the mode store, `line-approved.json` or `invites.json`
  is still manual SSH.
- **Group auto-registration.** `join`/`memberJoined` arrive and are discarded
  (adapter.py:1191), so adding the bot to a group still needs an env edit +
  restart. A group-pairing store (same no-restart shape as the mode store)
  would remove that.
- **Group member display names.** `user_name=user_id` (:1315) means the agent
  sees raw `U…` IDs per speaker — worse in a group than a DM.
- **Upstream PR.** Mention-gating for LINE is a real gap; the thin patch is the
  artifact to offer Nous once field-verified.
