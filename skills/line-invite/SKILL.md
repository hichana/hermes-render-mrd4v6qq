---
name: line-invite
description: "Use when an already-authorized user asks to invite/onboard someone new to LINE (e.g. after interviewing a candidate) — generates a one-off QR code that grants the scanner immediate LINE access with no separate approval step."
version: 1.0.0
author: Hermes Render
license: MIT
metadata:
  hermes:
    tags: [line, onboarding, pairing, qr, invite]
    related_skills: []
---

# LINE Join Invite (manager-initiated QR)

## Overview

Hermes' LINE adapter supports two ways an unrecognized DM becomes
authorized: the generic pairing-code handshake (`hermes pairing approve
line <code>`, unchanged, always available), and this skill's one-off QR
invite. The invite exists for the case where the access decision is
already made *before* the candidate ever messages the bot — e.g. you just
interviewed someone and hired them. Generate a QR, hand it to them, they
scan it and are in immediately. No one has to separately approve a
pairing code afterward.

This is safe to expose to any user this skill loads for, because a skill
only loads inside a conversation the agent is already having — and an
unauthorized LINE DM never reaches the agent loop at all (it gets dropped
or gets a pairing code, never a live agent turn). So "whoever this skill
is running for" is by construction someone already authorized on this
channel.

## When to Use

- The current user (an already-authorized LINE contact, or any other
  authorized platform) asks to "invite," "onboard," or "add" someone new
  to LINE.
- Phrases like "generate a LINE invite for X", "I just hired someone, get
  them set up on LINE", "give me a QR code for the new cashier."
- **Don't use for:** approving a pairing code someone already has (that's
  `hermes pairing approve line <code>` — a different, existing flow this
  skill doesn't replace) or inviting to any platform other than LINE.

## Generating an invite

Run the bundled script with the Hermes venv's Python (needed for the
`qrcode` dependency and to import the patched `LineInviteStore`):

```
/opt/hermes/.venv/bin/python3 <this-skill-dir>/scripts/generate_invite.py \
  --label "Jamie - PT cashier" \
  --created-by <the-current-chat's-LINE-user-ID-if-on-LINE-else-omit> \
  --hours 48
```

- `--label`: whatever identifies this invite to a human later (name +
  role is typical). Shows up in the "they joined!" notification.
- `--created-by`: pass the *current* chat's LINE user ID if this
  conversation is itself happening on LINE (you'll have it from the
  message source). If the manager is talking to you on a different
  platform, omit it — the invite still works, you just won't get a
  redemption notification back through LINE.
- `--hours`: invite lifetime. Default 48h. Use a value that comfortably
  spans "interview now, candidate starts in a couple of days" without
  staying live indefinitely if the QR is lost or forwarded — don't set
  it past a few days without the user explicitly asking for longer.

On success the script prints one JSON line: `{"token", "url", "qr_path",
"label", "expires_at"}`. On failure (`LINE_BASIC_ID` unset, `qrcode`
missing) it prints `{"error": "..."}` and exits 1 — read the error and
relay it to the user rather than retrying blindly; both failure modes
need an operator fix (an env var or a rebuild), not a different flag.

**Completion criterion:** you have the `qr_path` and have sent that image
back to the current user (the manager) in this chat, with a short note
of what it's for and when it expires. Do not just describe the invite —
send the actual QR image.

## What happens on scan (informational — you don't need to do anything else)

The candidate scans the QR → LINE opens a chat with the bot, pre-filled
with the invite token → they tap Send → the LINE adapter matches the
token, grants access immediately via the same approval path a pairing
code would (`PairingStore._approve_user`), and replies to them with a
welcome message. If `--created-by` was set, the creator also gets a "✅
`<label>` just joined via your invite!" message. None of this requires
another skill invocation or any further action from you.

## Common Pitfalls

1. **Forgetting `LINE_BASIC_ID`.** This is the channel's public Basic ID
   (LINE Developers Console → Messaging API → Basic Settings), not the
   channel access token or secret. If unset, the script fails loudly
   rather than generating a broken link — don't work around it, tell the
   user to set it.
2. **Describing the invite instead of sending the QR image.** The
   candidate needs to *scan* something. Text describing "an invite was
   created" is not a completed task.
3. **Treating this as a substitute for `hermes pairing approve`.**
   Someone who messages the bot *without* a valid invite still gets the
   normal pairing-code flow, unchanged — this skill only adds a second,
   faster path for the pre-vetted case.
4. **Reusing a token.** Each token is single-use by design (the store
   marks it redeemed on first match) — mint a fresh invite per person,
   don't reuse or hand out the same QR to multiple candidates expecting
   it to work more than once.

## Verification Checklist

- [ ] `LINE_BASIC_ID` was set (script didn't error)
- [ ] The QR PNG at `qr_path` was sent to the current user, not just described
- [ ] `--created-by` was passed if this conversation is itself on LINE
- [ ] The invite's expiry was mentioned to the user
