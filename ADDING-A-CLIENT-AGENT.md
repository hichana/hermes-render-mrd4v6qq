# Adding a second client agent (LINE channel) to an existing container

How to add another client-facing LINE agent to a Render service that's
already running one — a new Hermes **profile**, with its own persona,
memory, and its own LINE channel, multiplexed onto the same gateway
process as the existing default profile. See `SERVICES.md` for SSH
access and `ARCHITECTURE.md`'s "Multi-profile architecture" section for
the underlying model before starting.

**This is LINE-specific.** LINE is a *port-binding* platform (its own
webhook server, one fixed credential pair per instance upstream), which
is why it needed `patches/line-multi-channel.patch` — see
`plans/line-multi-channel-plan.md` — before a second client could get
their own channel at all. A second profile on a *polling* platform
(Telegram, Discord, Slack, Signal, …) needs none of this: multiplexing
already supports one bot token per profile natively (`gateway/run.py`'s
`_start_secondary_profile_adapters`), so skip straight to Step 2 for
those and give the new profile its own bot token in its own `.env`.

**Prerequisite:** the container must actually be running
`line-multi-channel.patch`. Check:

```bash
ssh -i ~/.ssh/render_hermes srv-d97k2t57vvec73ccpg2g@ssh.oregon.render.com \
  "grep -q _channel_for_send /opt/hermes/plugins/platforms/line/adapter.py && echo present"
```

If that doesn't print `present`, this instance hasn't been redeployed
onto the patched image yet — do that first (`UPGRADING.md`'s Phase 3
deploy step for this specific bump), and run this instance through
`plans/line-multi-channel-plan.md`'s Phase 7 Stage 1 (default-channel-only
no-op proof) before adding a real second channel here.

Also confirm `gateway.multiplex_profiles: true` is set (`grep -A1
'^gateway:' /opt/data/config.yaml`) — it already is on `ngraph-main` as
of 2026-08-01, but don't assume that for a different instance.

---

## Step 0 — Pick the profile name now

This name is used in **three places that must match exactly**: the
Hermes profile directory, the `channels[].profile` key in the default
profile's `config.yaml` (Step 4), and the LINE webhook path
(`/line/p/<name>/webhook`, Step 6). Decide it before starting so you're
not renaming things mid-setup.

Rules (`hermes_cli/profiles.py`'s `_PROFILE_ID_RE`, verified against the
live source): lowercase, starts with a letter or digit, then any mix of
lowercase letters/digits/`-`/`_`, up to 64 chars. Not `default` (reserved).
Use the client's own short name/slug — it'll show up in `hermes profile
list`, log lines, and this instance's `served_profiles` state, so make it
something an operator recognizes at a glance later.

## Step 1 — Create the LINE channel (LINE Developers Console)

1. [developers.line.biz/console](https://developers.line.biz/console/) →
   select the Provider (or create one for this business) → **Create a new
   channel** → **Messaging API**.
2. Fill in the channel name/description/category using the real business
   name — this is client-facing.
3. **Basic settings** tab → note the **Channel secret**. Treat it as a
   secret from this point on.
4. **Messaging API** tab → **Issue** a long-lived **Channel access
   token**. Note it — also a secret, and LINE only shows/lets-you-copy it
   at issue time (you can always re-issue a new one later, invalidating
   the old).
5. Same tab → note the **Bot Basic ID** (e.g. `@abc1234`) — only needed if
   this business also wants the `line-invite` skill (manager-initiated QR
   join invites); it goes in the new profile's own `.env` as
   `LINE_BASIC_ID` in that case, per `skills/line-invite/SKILL.md`.
6. Open **LINE Official Account Manager** from that same tab → **Response
   settings** → turn **off** "Greeting messages" and "Auto-reply
   messages". Without this, LINE's own canned replies race the agent's
   real reply on every new conversation — standard setup for any Hermes
   LINE channel, not specific to multi-channel.
7. **Leave the Webhook URL blank for now.** You need the container to
   have a live route to verify against first — that's Step 6, after the
   profile and config exist.

## Step 2 — Create the Hermes profile

Every command below through Step 4 runs **on the instance** — either
paste it as its own `ssh ... "<command>"` one-liner (shown that way
below) or SSH in once and run the bare command inside that session,
whichever's more convenient. Step 5 switches to your own machine.

```bash
ssh -i ~/.ssh/render_hermes srv-d97k2t57vvec73ccpg2g@ssh.oregon.render.com \
  "/command/s6-setuidgid hermes /opt/hermes/.venv/bin/hermes profile create <name> --clone"
```

(`--clone` copies `config.yaml`, `.env`, `SOUL.md`, and installed skills
from the currently-active profile — normally `default` — which is the
easy way to inherit the same LLM provider key and toolset rather than
re-entering them. Omit it for a fully bare profile if you'd rather start
from scratch.)

### The one landmine here, verified against the actual source

`--clone` copies the **whole** `.env`, including `LINE_CHANNEL_ACCESS_TOKEN`
and `LINE_CHANNEL_SECRET` if the profile you cloned from has LINE
configured via env vars — which `default` does on this instance (its
LINE config has always been pure `.env`, no `platforms.line` block in
`config.yaml` at all). If those two keys land in the **new** profile's
own `.env`, the gateway's port-binding check
(`gateway/run.py::_start_one_profile_adapters`, reads that profile's
`config.yaml` **as resolved through its own env**, so this really is
driven by the cloned `.env`, not a `platforms:` block you'd have to add
by hand) sees LINE as "enabled" for that profile and raises
`SecondaryPortBindingConfigError` — which **skips starting every
adapter for that profile, silently**, logging only one `WARNING` line.
The profile exists, looks fine in `hermes profile list`, and simply never
appears in `served_profiles` after a restart. Nothing in the dashboard
flags this.

**Fix, immediately after `--clone`, before ever restarting the gateway:**

```bash
/command/s6-setuidgid hermes sed -i '/^LINE_CHANNEL_ACCESS_TOKEN=/d;/^LINE_CHANNEL_SECRET=/d' \
  /opt/data/profiles/<name>/.env
```

(Same for any *other* port-binding platform's required env vars if the
profile you cloned from has one configured — `WHATSAPP_CLOUD_*`,
`MSGRAPH_WEBHOOK_*`, etc. LINE is almost certainly the only one on this
instance, but check `grep -E '^[A-Z_]+_(TOKEN|SECRET|KEY)=' /opt/data/.env`
against `gateway/config.py`'s `PORT_BINDING_PLATFORM_VALUES` if unsure.)

This LINE channel's own credentials belong **only** in the default
profile's `config.yaml` `channels` entry (Step 4) — the new profile
never runs its own LINE adapter at all. Its job is purely to exist as a
`profiles/<name>/` directory that `source.profile` can route config,
memory, and sessions into; inbound LINE messages always arrive through
the default profile's one `LineAdapter` instance, which stamps
`source.profile` in software (`plans/line-multi-channel-plan.md`'s core
mechanism) rather than routing at the platform-adapter level the way
Telegram/Discord secondary profiles do.

**Why this is a smaller risk than it sounds, but not zero**: upstream's
own `_profile_runtime_scope`/`agent.secret_scope.build_profile_secret_scope`
mechanism (`gateway/run.py`, read directly on this pinned tag) exists
specifically so a secondary profile's config resolution reads *that
profile's own* `.env` and never falls through to the process-global
`os.environ` — the exact class of cross-profile env leak upstream issue
[#50051](https://github.com/NousResearch/hermes-agent/issues/50051)
described appears to be fixed by this mechanism on `v2026.7.20`. That
means the *default* profile's own LINE credentials sitting in
`/opt/data/.env` do **not** leak into the new profile's enablement check
— the risk above is specifically and only about what a `--clone` copies
into the *new* profile's own file. Still worth the Step 5 verification
below rather than trusting this from reading code alone.

### Configure the new agent

Set up `SOUL.md` (persona), `config.yaml` (model, tools, skills) for
`profiles/<name>/` the same way you would for any new client instance —
this part isn't new to multi-channel. Do it now, before wiring the LINE
channel in, so the first real message has something coherent to talk to.

## Step 3 — Back up config.yaml

Same discipline as every other hand-edited store on this instance
(`SERVICES.md`):

```bash
/command/s6-setuidgid hermes cp /opt/data/config.yaml /opt/data/config.yaml.bak-$(date +%Y%m%d)
```

## Step 4 — Add the channel to the default profile's config.yaml

This instance's `config.yaml` currently has **no `platforms:` block at
all** (LINE's default-channel config has always been pure `.env`) — so
this is adding a new top-level section, not editing an existing one.
Edit `/opt/data/config.yaml` (dashboard's config editor, or
`/command/s6-setuidgid hermes vi /opt/data/config.yaml` over SSH) and add:

```yaml
platforms:
  line:
    extra:
      channels:
        - profile: <name>                    # must match Step 0/2 exactly
          channel_secret: "<channel secret from Step 1.3>"
          channel_access_token: "<channel access token from Step 1.4>"
          # Optional, all default sensibly if omitted — see
          # modules/line/line_multiplex.py and plugin.yaml's comment block:
          # allowed_users: ["Uxxxxxxxx"]
          # allowed_groups: []
          # allowed_rooms: []
          # allow_all_users: false
          # dm_policy: pairing               # open | allowlist | disabled | pairing
```

This is a **list** — a second future channel is another entry under
`channels:`, not a replacement of this one. The default channel itself
is untouched: it stays entirely `.env`-driven exactly as today, and
isn't (and can't be) represented in this `channels` list — see
`plugin.yaml`'s comment block for the schema.

Validate the YAML before restarting anything:

```bash
/command/s6-setuidgid hermes python3 -c "import yaml; yaml.safe_load(open('/opt/data/config.yaml'))" \
  && echo "YAML OK"
```

## Step 5 — Restart and verify

```bash
uv run --project admin-tools/env-sync hermes-env-sync restart-only <slug>
```

(`<slug>` is this instance's entry in `clients/registry.yaml` — see
`admin-tools/env-sync/README.md`. This is the *verified* restart path:
it confirms both `pid` and `start_time` in `/opt/data/gateway.pid`
actually changed, not just that a restart was requested. Don't trust
`hermes gateway restart` or the dashboard's restart button —
`ARCHITECTURE.md` documents both as silent no-ops on this deployment.)

Then confirm the new profile actually came up — this is the check that
catches Step 2's landmine if it wasn't fully fixed:

```bash
ssh -i ~/.ssh/render_hermes srv-d97k2t57vvec73ccpg2g@ssh.oregon.render.com \
  "cat /opt/data/gateway_state.json | python3 -m json.tool | grep -A5 served_profiles"
```

`served_profiles` must include `<name>`. If it doesn't, check
`/opt/data/logs/gateway.log` for `SecondaryPortBindingConfigError` or
`MultiplexConfigError` mentioning that profile name — the fix is almost
certainly Step 2's `.env` strip that didn't fully land.

Also confirm the new route registered, from inside the container (LINE
isn't reachable from outside until Step 6 sets the real webhook URL, but
the health route works immediately):

```bash
ssh -i ~/.ssh/render_hermes srv-d97k2t57vvec73ccpg2g@ssh.oregon.render.com \
  "curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:10000/line/p/<name>/webhook/health"
```

Expect `200`. `404` means the profile didn't come up (see above); `502`
means the route pattern is wrong somewhere (shouldn't happen if the
`profile:` value matches Step 0/2/4 exactly).

## Step 6 — Point the LINE channel at the new route

Back in the LINE Developers Console (Step 1's channel), **Messaging
API** tab → **Webhook settings**:

- **Webhook URL**: `<same base URL as your default channel's webhook>/p/<name>/webhook`
  — i.e. if the default channel's console has
  `https://<host>/line/webhook`, this one is
  `https://<host>/line/p/<name>/webhook`. Look up `<host>` from this
  instance's own `LINE_PUBLIC_URL` if unsure:
  `grep LINE_PUBLIC_URL /opt/data/.env`.
- Click **Verify** — LINE POSTs an empty event batch and expects `200`.
  This is the live equivalent of Step 5's health check, now proving the
  public path through Render + Caddy, not just loopback inside the
  container.
- Toggle **Use webhook** on.

## Step 7 — End-to-end test

- Message the **new** channel's QR/friend link from a phone. Confirm a
  reply comes from the new agent's own persona (Step 2's `SOUL.md`), not
  the default profile's.
- Message the **default** channel too, in the same pass. Confirm it
  still replies exactly as before — this is the regression check that
  matters most (`plans/line-multi-channel-plan.md`'s Phase 9: a
  cross-channel isolation failure, in either direction, is a security
  incident, not an ordinary bug).
- If this business also wants manager-initiated QR invites, confirm
  `LINE_BASIC_ID` is set in `profiles/<name>/.env` (Step 1.5) and the
  `line-invite` skill works from a chat with that agent.

## Troubleshooting quick reference

| Symptom | Likely cause |
|---|---|
| New profile missing from `served_profiles` after restart | Step 2's landmine — port-binding env var(s) survived in the new profile's `.env`. Check `/opt/data/logs/gateway.log` for `SecondaryPortBindingConfigError`. |
| `/line/p/<name>/webhook/health` returns 404 | Same as above, or a typo in `profile:` (config.yaml) vs. the actual profile directory name — they must match byte-for-byte. |
| LINE console's webhook verify fails, but the loopback health check (Step 5) passed | Public routing problem, not this patch — check `LINE_PUBLIC_URL`, Render's own reachability, Caddy logs. Not something this guide's steps would cause. |
| New channel's messages get a reply, but it's the **default** agent's persona | The webhook URL in the LINE console (Step 6) is still pointing at `/line/webhook` instead of `/line/p/<name>/webhook` — a copy-paste of the default channel's URL. |
| A message on the new channel gets **no** reply at all, and the health check passes | Check the new channel's `dm_policy`/allowlists (config.yaml, Step 4) — same allowlist semantics as the default channel, just scoped to this one entry. |

## What's still manual, and why

`admin-tools/env-sync` does not manage `platforms.line.extra.channels` —
it only ever upserts flat `.env` keys, and this config is a structured
YAML list that carries secrets, which is exactly the kind of change that
tool exists to make safe and this guide's Steps 3-5 have to do by hand
instead (backup, edit, validate, verified restart). Extending it to cover
this is scoped as its own follow-up in
`plans/line-multi-channel-plan.md`'s Phase 5 — not started as of this
writing. Until then, this document is the process.
