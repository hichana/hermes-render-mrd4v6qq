## A non-default LINE channel's unauthorized-DM pairing code silently never sent

**Found live, 2026-08-02, right after fixing the #50051 env-leak entry below** — with that fixed, `ask-ngraph`'s messages reached authorization correctly, got logged as `Unauthorized user: ... on line` (expected — nobody had approved that user for this channel yet), but the "Hi~ I don't recognize you yet! Here's your pairing code" reply that should follow never arrived. No error, no exception, no extra log line — the "thinking" ellipsis on LINE just showed forever.

Root cause, traced in `gateway/authz_mixin.py::_authorization_adapter`: it resolves which live adapter should send the pairing-code reply via `runner._profile_adapters[<profile>][platform]`, and **fails closed** — deliberately, per its own comment ("must NOT fall back to the default profile's adapter — that sends replies out the wrong bot") — when nothing is registered there. For LINE under this repo's multi-channel patch, nothing ever *is* registered there for a non-default channel: there is exactly one `LineAdapter` instance, living only under the default profile's `self.adapters` (a non-default channel's own `config.yaml` has LINE explicitly disabled — see the #50051 entry below), so `_authorization_adapter("ask-ngraph", LINE)` correctly finds nothing and returns `None`. `gateway/run.py`'s pairing-code sender then does `adapter = self._adapter_for_source(source); if adapter: await adapter.send(...)` — `adapter` is `None`, the `if` is false, nothing is sent, nothing is logged.

The fail-closed behavior is *correct* for the normal case (Telegram/Discord-style: one real adapter instance per profile per platform — using the wrong one really would send from the wrong bot). It's wrong specifically for this repo's shared-adapter LINE design, where `send()` never trusts `profile` for credential resolution at all — only `chat_id`, via `ChannelRegistry.channel_for_chat()`, which is unambiguous because LINE's own IDs are channel-scoped.

Fix: `LineAdapter.connect()` now registers the one shared adapter instance under `runner._profile_adapters[<profile>][Platform.LINE]` for every additional channel too (reachable via the `gateway_runner` back-reference every adapter gets, already used by `build_source()`) — so the generic resolution finds it, and its own internal `chat_id`-based routing does the rest correctly regardless of which profile "claimed" it. `disconnect()` undoes exactly that registration. Covered by `TestProfileAdapterRegistration` in `patches/line-multi-channel.tests.patch`.

The transferable shape: **a "fail closed, never guess" safety check is only as correct as its assumption about what "the right adapter" means** — here it assumed 1:1 profile-to-adapter, which is true everywhere in the codebase except the one place this repo intentionally shares an adapter across profiles. When bypassing a generic multi-tenancy assumption for a legitimate reason, expect every *other* piece of code built on that same assumption to need the identical bypass, not just the one you found first.

## Upstream #50051's env leak is real — a secondary profile inherits the default profile's platform credentials via `os.environ`, not just its own `.env`

**Found live, 2026-08-02, while provisioning the first real second LINE channel.** An earlier research pass (`research/07`, then this repo's `ADDING-A-CLIENT-AGENT.md`) read `gateway/run.py::_profile_runtime_scope`'s docstring — which explicitly claims a secondary profile's config resolution reads *that profile's own* `.env` and never falls through to the process-global `os.environ` — and concluded upstream issue [#50051](https://github.com/NousResearch/hermes-agent/issues/50051)'s env-leak bug was probably fixed on this pinned tag. **Wrong**, and confirmed wrong by creating a real profile (`ask-ngraph`) and watching it fail twice:

1. `hermes profile create ask-ngraph --clone` copies the *whole* `.env`, including `LINE_CHANNEL_ACCESS_TOKEN`/`SECRET`. Stripped those from `ask-ngraph`'s own `.env`, restarted — **still** got `WARNING gateway.run: Skipping secondary profile 'ask-ngraph' due to port-binding config error: ... enables port-binding platform(s) line`, even though its own `.env` had zero LINE vars left.
2. Root cause, traced in `gateway/config.py`'s registry-driven platform-enablement loop: it calls each plugin's `env_enablement_fn`/`is_connected` to auto-detect "is this configured", and LINE's implementation (like most built-ins) calls `os.getenv(...)` **directly** — not the `get_secret()`/`agent.secret_scope` abstraction `_profile_runtime_scope` actually protects. Hermes loads the *default* profile's `.env` into the real process environment at gateway startup (`override=True`) and never clears it, so `LINE_CHANNEL_ACCESS_TOKEN`/`SECRET` stay visible via `os.environ` to *every* profile's config resolution for the life of the process, regardless of that profile's own `.env` contents.
3. Fixed the port-binding case by adding an explicit `platforms: line: enabled: false` to `ask-ngraph`'s own `config.yaml` — `gateway/config.py` checks for an explicit `enabled: false` *before* ever calling `env_enablement_fn`, so this genuinely short-circuits the leak rather than racing it. Restarted, `SecondaryPortBindingConfigError` gone, `ask-ngraph` appeared in `served_profiles` — but a **new** warning appeared: `✗ telegram failed to connect (profile: ask-ngraph)`, for the identical reason (`TELEGRAM_BOT_TOKEN` leaking the same way, Telegram just isn't port-binding so it degrades to a failed-connection warning instead of skipping the whole profile). Added `platforms: telegram: enabled: false` too. Clean boot after that — confirmed via a completely quiet startup log and `served_profiles: ["default", "ask-ngraph"]`.

**The transferable rule, going forward:** every new profile's own `config.yaml` needs an explicit `enabled: false` for **every** platform the default profile has configured via `.env` — not just the port-binding one you're trying to add multi-channel support for. Check `/opt/data/.env` for the real set on a given instance; don't assume it's just LINE. Stripping the new profile's own `.env` of those same credential vars is still worth doing as defense-in-depth, but it does **not**, on its own, prevent this — the leak comes from the *default* profile's environment, not the new profile's file, so there was never anything in the new profile's own `.env` for the strip to fix in the first place.

The transferable shape: **a docstring's claim about what a scoping mechanism protects is not evidence about what it doesn't.** `_profile_runtime_scope`'s claim was true and specific to `get_secret()` — reading it as "config resolution is safe" rather than "*this one thing* is safe" produced a documented recommendation that failed on the very first real use. Treat a scoping/isolation claim as covering exactly what it says it covers, and verify empirically before relying on it for anything wider.

## Stamping `source.profile = "default"` broke DM pairing for the default LINE channel

**Found live, 2026-08-02, immediately after deploying `line-multi-channel.patch`.** The health checks, `hermes doctor`, and a fresh `docker build`/smoke-test all passed — then an already-approved user messaged the default channel and got re-issued a brand-new pairing code, as if they'd never been approved.

The patch's `_handle_message_event` stamped `source_obj.profile = current_channel.profile` for every channel, including the default one (`profile == "default"`). That looked harmless — `gateway/run.py::_resolve_profile_home_for_source` treats an explicit `"default"` and an unset `None` identically, resolving to the same `HERMES_HOME`. But `gateway/authz_mixin.py::_pairing_store_for()` doesn't: it routes to a **per-profile** `PairingStore` whenever `source.profile` is truthy *and* present in `self.pairing_stores` — and the multiplexer's own bootstrap (`_start_secondary_profile_adapters`) unconditionally registers `"default"` as a key there (`served = [active] + ...`). So stamping the literal string `"default"` silently redirected every default-channel message to `PairingStore(profile="default")` — a real, distinct, and (on a fresh instance) **empty** store at `profiles/default/pairing/` — instead of falling through to the correct global store at `<HERMES_HOME>/platforms/pairing/`, where the actual approvals live.

What actually proved it: reading `gateway/authz_mixin.py::_pairing_store_for` directly and cross-referencing `/opt/data/profiles/default/pairing/` (empty) against `/opt/data/platforms/pairing/line-approved.json` (had the real, already-approved user). The two health/build/smoke checks that passed only prove the patch *loads*, not that every downstream consumer of `source.profile` treats an explicit `"default"` the same as an absent one — and this is the one place in the whole gateway that doesn't.

Fixed: only stamp `source_obj.profile` for **non-default** channels; leave it unset for the default channel, exactly matching pre-patch behavior. Covered by two regression tests in `patches/line-multi-channel.tests.patch` (`test_default_channel_does_not_stamp_a_profile`, plus a variant with a second channel configured, to prove the exclusion is keyed on "is this the default channel" and not "is this the only channel").

The transferable shape: **a value that's semantically equivalent in one consumer isn't guaranteed equivalent in every consumer.** `None` and `"default"` were interchangeable for HERMES_HOME resolution and looked interchangeable everywhere at a glance — they weren't, in the one place that mattered for user-facing correctness. When stamping a field a base class already reads in several places, grep for *every* reader before assuming your value's meaning is uniform.

## The boot-time seeder is gone

There used to be a `scripts/seed-env-from-render.py` cont-init step that copied a couple of Render Environment-tab vars into `.env` on boot. It was **insert-only** (never overwrote), so it worked exactly once per key and then went permanently, silently inert — edit the Render tab afterwards and nothing happened. It was removed in favor of `env-sync`. Don't reintroduce a boot-time seeder for this.

One live incident from it is still worth carrying forward: cont-init hooks under s6-overlay v3 do **not** see Render/docker `-e` env vars unless the shebang is `#!/command/with-contenv sh`. The seeder saw an empty value for its var purely because `bootstrap.sh` used plain `#!/bin/sh`.

## A red preflight looked like upstream drift but was GitHub rate limiting

**Found during the `v2026.7.7.2` → `v2026.7.20` bump (2026-07-30).** The first `scripts/upgrade-preflight.sh` run reported `MISSING(blocker): docker/main-wrapper.sh — not present at v2026.7.20` — the ENTRYPOINT chain, i.e. the single scariest thing that check can say, and precisely the `c113823` failure class. It also inflated the churn counts on several `review` files (`gateway/run.py` at 3359 changed lines, the whole file).

None of it was real. `curl -sI` on that exact URL returned **HTTP 200**: upstream ships the file unchanged. GitHub had answered **429** to the burst of raw fetches, and the script used `curl -sfL`, which collapses every non-2xx into "no file written" — indistinguishable from a genuine 404. A rate-limited run therefore produced a *confidently wrong* verdict, and following it would have meant reworking patches against an upstream move that never happened.

What actually proved it: `curl -sI <same-url>` per suspicious path, checking the status code directly rather than re-running the script. A second run after the limit cleared came back `1 blockers, 6 to review`, matching UPGRADING.md's recorded worked example exactly.

Fixed in `scripts/upgrade-preflight.sh`: `fetch()` now reads `%{http_code}` and returns a tri-state — fetched / genuinely 404 / **couldn't tell** — and a "couldn't tell" aborts the whole run with exit 3 and prints no verdict at all, rather than reporting drift it never measured. `tests/test_upgrade_preflight.py::TestTransientFetchFailure` covers it over a real local HTTP server, since `file://` can't express a 429.

The transferable shape: **this file's usual lesson is "green didn't mean healthy." This is the mirror image — red didn't mean broken.** A checker that can't distinguish "the answer is no" from "I couldn't ask" will eventually tell you the scariest version of the story. When a preflight blocker and the recorded history disagree, verify the blocker against the artifact before believing it.

## Deleting a profile safely

**Confirmed in practice (2026-07-24): `hermes profile delete <name>` does not reliably kill the profile's running gateway process.** It removes the profile's files and updates Hermes' own bookkeeping (`hermes profile list` will report it "stopped" or gone entirely), but the underlying OS process can keep running. On Render, you can't clean up after this the way you would locally — SSH access there runs as UID 0 but **without `CAP_KILL`** into the main container's process tree, so `kill`/`os.kill()` against the orphaned PID fails with `PermissionError: Operation not permitted` even as root, and `s6-svc -d` fails with "No such file or directory" if the service's control path was already torn down by the deletion.

Left alone, that orphaned process is actively harmful, not just idle: through its own normal housekeeping writes (session state, logs, `gateway_state.json`), it will **recreate its own profile directory** on disk within minutes. The next container restart's boot-time reconciler (`hermes_cli/container_boot.py`, wired as `/etc/cont-init.d/02-reconcile-profiles`) walks every directory under `$HERMES_HOME/profiles/` and re-registers an s6 service for anything with a `SOUL.md` — so it will faithfully resurrect a `gateway-<name>` service for a profile you already "deleted."

Safe procedure:

1. **Stop the gateway first, and verify it's actually gone**, before deleting anything:
   ```bash
   /opt/hermes/.venv/bin/hermes -p <name> gateway stop
   ps aux | grep "hermes -p <name>"   # must print nothing
   ```
2. Only then run `hermes profile delete <name> --yes`.
3. If step 1's `ps` check ever does show a lingering process after `gateway stop` (or you skip straight to `delete` and find one afterward) — don't fight it over SSH. **Restart the service from the Render Dashboard** (or `render deploys create` a no-op redeploy). This is the only reliable way to clear an orphaned gateway process on Render; the container's tmpfs `/run/service/` is wiped on restart, and the reconciler will not recreate a slot for a profile whose directory is genuinely gone.
4. After the restart, confirm with `hermes profile list` that only the profiles you expect are present, and re-run step 2's deletion again if the directory got resurrected in the gap before the restart.

If you want the conversation history a deleted profile was holding (sessions, memories), get it out *before* deleting:

```bash
hermes profile export <name>                              # full backup archive, cheap insurance
hermes -p <name> sessions export --format md \
  --session-id <id> <output-dir> --yes                     # human-readable transcript
```
Memory files (`memories/USER.md`, `memories/MEMORY.md`) are just markdown — worth diffing against the profile you're keeping and merging anything genuinely useful (not boilerplate carried over from a `--clone`) before the source profile is gone.

