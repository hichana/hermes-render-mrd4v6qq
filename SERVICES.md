## Debugging a deployed instance over SSH (admin-only)

This is separate from the image itself (which, per above, has zero Render account access baked in) — it's Matt/admin tooling for inspecting a running instance from the outside, e.g. reading pairing/allowlist state that only exists on the instance's persistent volume, not in this repo.

- **Key**: `~/.ssh/render_hermes` (private) / `~/.ssh/render_hermes.pub`.
- **Connect**: `ssh srv-d97k2t57vvec73ccpg2g@ssh.oregon.render.com`
- Render's SSH gateway may throw a `bad signature for ED25519 key` warning on the *host* key during connect (proxy behavior, not a MITM) — noisy but harmless; it still authenticates and connects fine.
- Once in, Hermes' persistent state lives under `/opt/data`, owned by the `hermes` user. Relevant to LINE pairing/allowlist debugging specifically:
  - `/opt/data/platforms/pairing/line-approved.json` — approved LINE user IDs + display names (this is the actual "allow list" for LINE DMs under `dm_policy: pairing`; see `patches/line-dm-pairing.patch`).
  - `/opt/data/platforms/pairing/line-pending.json` — outstanding pairing codes awaiting operator approval (may contain stale entries for since-approved users; harmless).
  - `/opt/data/platforms/pairing/_rate_limits.json` — pairing-attempt rate-limit counters. Bookkeeping; nothing to hand-edit.
  - `/opt/data/platforms/line-invites/invites.json` — one-off QR invite tokens from the `line-invite` skill, redeemed or not.
  - `/opt/data/platforms/line-modes/modes.json` — per-group response mode (`mention` = reply only when @-mentioned, `always` = reply to everything), plus who set it and when. Absent entry means the group follows the `LINE_REQUIRE_MENTION` default (mention-required when that var is unset). **The whole `line-modes/` directory is absent until the first mode is set** — verified 2026-07-30, it does not exist on this instance. That is normal, not a lost volume; don't go looking for a bug when `ls` fails here. This is the *only* place a group's mode lives — it is not mirrored in `.env`, and it is re-read on every message, so editing it takes effect immediately with no restart. Same backup-then-edit-then-confirm discipline as the other JSON stores if you touch it by hand; normally it's driven from inside the chat instead.
- Treat this as read-only unless the task explicitly calls for a change — this is a live client-facing instance, not a dev box.

## Managing per-client env vars and secrets

The manual SSH poking above is for ad-hoc, read-only debugging. For deliberate, ongoing changes to a client's business config or secrets (LINE tokens, allowlists, `LINE_BASIC_ID`, etc.), use [`admin-tools/env-sync`](admin-tools/env-sync/README.md) instead — a different, scripted SSH-based workflow that does a targeted upsert into `/opt/data/.env` plus a verified gateway restart, rather than a hand edit.

