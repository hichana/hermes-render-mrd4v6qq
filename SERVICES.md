## Debugging a deployed instance over SSH (admin-only)

This is separate from the image itself (which, per above, has zero Render account access baked in) — it's Matt/admin tooling for inspecting a running instance from the outside, e.g. reading pairing/allowlist state that only exists on the instance's persistent volume, not in this repo.

- **Key**: `~/.ssh/render_hermes` (private) / `~/.ssh/render_hermes.pub`.
- **Connect**: `ssh srv-d97k2t57vvec73ccpg2g@ssh.oregon.render.com`
- Render's SSH gateway may throw a `bad signature for ED25519 key` warning on the *host* key during connect (proxy behavior, not a MITM) — noisy but harmless; it still authenticates and connects fine.
- Once in, Hermes' persistent state lives under `/opt/data`, owned by the `hermes` user. Relevant to LINE pairing/allowlist debugging specifically:
  - `/opt/data/platforms/pairing/line-approved.json` — approved LINE user IDs + display names (this is the actual "allow list" for LINE DMs under `dm_policy: pairing`; see `patches/line-dm-pairing.patch`).
  - `/opt/data/platforms/pairing/line-pending.json` — outstanding pairing codes awaiting operator approval (may contain stale entries for since-approved users; harmless).
  - `/opt/data/platforms/line-invites/invites.json` — one-off QR invite tokens from the `line-invite` skill, redeemed or not.
- Treat this as read-only unless the task explicitly calls for a change — this is a live client-facing instance, not a dev box.

