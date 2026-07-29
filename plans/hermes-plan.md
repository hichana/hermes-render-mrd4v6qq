# Hermes Agent Deployment Checklist — One Business, Multiple Virtual Employees

This is a repeatable per-business checklist, uses profile distributions to avoid re-deriving config from scratch each time (see Phase 6).

## Phase 0 — Confirm before building

- [ ] **Test `session_search` cross-employee scoping on a throwaway instance first.** Have two test "employees" each tell a shared virtual employee a distinct fact in separate DMs, then in a third session ask a phrasing that would trigger `session_search` about "what did the other person say." The docs don't confirm this tool is scoped per user, and there's an open GitHub issue on exactly this gap — don't assume it's safe for sensitive info until you've seen it behave correctly yourself.
- [ ] **Test whether Render's plan actually gives you shell/SSH access** to the running service before committing to that as your only admin path. Confirm this works before you disable terminal access everywhere else.
- [ ] **Test the Docker-sandbox-mirror edge case** if you ever do enable `terminal.backend: docker` for any profile (e.g., a future admin profile) — there's a known bug where file writes can silently land in the sandbox mirror instead of the real profile directory.
- [ ] Decide, per virtual employee, whether its group chats will be shared-context (`group_sessions_per_user: false`) or per-user-private (`true`, the default) — remember this is one on/off switch for *all* of that virtual employee's groups, not settable per individual channel. If you want a shared-context sales channel and a private-per-user support channel, that likely needs to be two separate virtual employees (profiles), not one.
- [ ] **Understand the session-key model so you set the right isolation knob.** Hermes keys sessions like this:

  | Chat type | Session key pattern | Behavior |
  |---|---|---|
  | DM | `agent:main:<platform>:dm:<chat_id>` | Always one private session per person — no setting affects this |
  | Group/channel | `agent:main:<platform>:group:<chat_id>:<user_id>` | Per-user session inside the shared group when the platform exposes a user ID; controlled by `group_sessions_per_user` |
  | Group thread/topic | shared by default, or per-user if configured | `thread_sessions_per_user: true` splits a thread/topic per user |

  `group_sessions_per_user` governs top-level group chats; `thread_sessions_per_user` is the *separate* knob for threads/topics inside a group. Decide both per virtual employee — don't assume the group setting also covers threads.

## Phase 1 — Per-business infrastructure (Render)

- [ ] Deploy one Render web service running Hermes-in-Docker per business (the [Hermes on Render template](https://render.com/templates/hermes-on-render)), giving business-to-business isolation at the container level.
- [ ] Size the persistent disk beyond the template's 5GB default if you expect many virtual employees × active sessions × Honcho cache — you can increase a Render disk at any time from the Disks tab with no downtime for the resize, but you can never shrink it, so start modest and re-check disk usage after your first couple of virtual employees are live.
- [ ] Confirm and document how you'll reach this service's shell/SSH as the admin — this is now your *only* terminal access path, so nail it down before you disable terminal tooling everywhere else.
- [ ] Set `HERMES_DASHBOARD=1` and configure a real auth provider (username/password at minimum) for the web dashboard — needed to manage API keys, profiles, and MCP connections per business without shell access.
- [ ] Confirm this Render service's terminal backend stays on `local` (default) rather than attempting `terminal.backend: docker` — Render is unlikely to expose a Docker socket for Docker-out-of-Docker, and Modal/Daytona (cloud sandbox APIs, no local socket needed) would be the fallback *if* you ever need sandboxed command execution for anything. Given the decision below to disable terminal for everyone, this may be moot — revisit only if that changes.

## Phase 2 — Virtual employee profiles

For each virtual employee at this business:

- [ ] `hermes profile create <name>` inside the instance (via dashboard or shell).
- [ ] Write a `SOUL.md` defining that virtual employee's persona/role.
- [ ] Set `terminal.cwd` if it needs a consistent working context (rare if terminal is disabled — see Phase 4).
- [ ] Configure its model/provider in `config.yaml`.
- [ ] Decide and set `group_sessions_per_user` for this profile (see Phase 0 decision) — default `true` (private-per-user in groups) unless you specifically want shared-context. If any of this virtual employee's work happens in group *threads/topics*, also set `thread_sessions_per_user` deliberately (it's a separate knob; `true` splits a thread per user). DMs are always private regardless of either setting.
- [ ] Provision a dedicated LINE Official Account/channel for this virtual employee (LINE Developers console), and put its channel token/secret in this profile's own `.env` — remember Hermes locks a token to one profile, so reusing a token across two virtual employees will fail loudly, which is a useful sanity check that you provisioned separate channels correctly.
- [ ] Repeat for Slack (a distinct Slack app/bot token per virtual employee, in the same `.env`).
- [ ] Add each authorized human employee via allowlist (`LINE_ALLOWED_USERS` / `SLACK_ALLOWED_USERS` equivalents) or, better at any scale beyond a handful of people, DM pairing (`hermes pairing approve <platform> <code>`) so you're not hand-collecting platform IDs for every hire.
- [ ] Set a home channel (`/sethome`) if this virtual employee will run scheduled/cron tasks that need somewhere to deliver output.

## Phase 3 — Memory: Honcho (Cloud, one workspace per business)

- [ ] Create one Honcho Cloud workspace per business (not shared across businesses — this is what keeps one business's employee identities and modeling separate from another's).
- [ ] `hermes memory setup` → select Honcho → configure with that business's workspace + API key, on the **first** virtual employee profile.
- [ ] **Neutralize the built-in memory so it can't blend employees underneath Honcho.** Hermes' built-in memory stays active *alongside* Honcho — Honcho layers on top, it doesn't replace it. The built-in single `USER.md` has no concept of "who said this," so it's the exact cross-employee blending risk Honcho exists to fix. For each employee-facing profile, set `memory_enabled: false` and `user_profile_enabled: false` in `config.yaml` so the agent stops writing a shared USER.md, and treat Honcho as the sole real memory layer. **Caveat (known bug — Hermes issues #45422 / #18404):** even when disabled, the built-in memory *prompt* may still be auto-injected, so this is not yet a clean off-switch. Because of that, during Phase 0 / Phase 8 testing, explicitly confirm the built-in USER.md is not quietly accumulating one employee's facts and applying them to everyone.
- [ ] For every additional virtual employee at this business, run `hermes profile create <name> --clone` (or `hermes honcho sync` for existing profiles) so each gets its own `aiPeer` under the same shared workspace — this is what gives each virtual employee an independent representation of the user(s) it talks to, rather than one blended model.
- [ ] **Gateway identity mapping — the part that actually solves your multi-employee problem:** set `userPeerAliases` in `honcho.json` to map each employee's platform runtime ID (LINE user ID, Slack user ID) to their own distinct peer name (e.g., `{"U012345": "priya", "U067890": "sam"}`). **Do not use `pinUserPeer: true`** — that setting collapses every human talking to a virtual employee into one shared peer identity, which would recreate the exact USER.md-blending problem Honcho is meant to solve for you.
- [ ] Leave `observationMode: directional` (default) unless you have a specific reason a virtual employee shouldn't self-model from its own replies.
- [ ] Decide `recallMode` per virtual employee — `hybrid` (auto-inject + tools) is the sensible default; `tools`-only if you want tighter control over when Honcho gets queried.
- [ ] Set a sensible `contextTokens` cap so Honcho's injected context doesn't balloon your per-turn token cost unbounded.

## Phase 4 — Guardrails: no destructive actions via chat, for anyone

- [ ] **Disable the `terminal` and `execute_code` tools entirely** for every virtual employee profile at this business, via `hermes tools` — this is the primary control, not the approval system. In Hermes, tool *registration* (the toolset existing system-wide) and tool *exposure* (which profile can actually call it) are separate concerns: you leave the toolset registered but simply do **not** expose shell/code execution to any employee-facing profile at all. If the tool isn't exposed, there's nothing for an employee (or a prompt injection) to trigger — you remove the whole risk category rather than gating it.
- [ ] Leave `approvals.mode: manual` (default) as a backstop in case any profile's toolset is changed later — never set `approvals.mode: off` on any employee-facing profile.
- [ ] Add `approvals.deny` glob rules for anything you'd never want run even by accident (force-pushes, piping curl to shell, etc.) — this is unconditional and can't be bypassed by "always approve"/yolo mode, so it's a good belt-and-suspenders layer even with terminal disabled, in case it's ever re-enabled for a specific task.
- [ ] Note the current limitation: Hermes has **no per-user role-based permissions inside a shared profile** (the open Owner/Admin/User/Guest RBAC feature request). Approval prompts route back to whoever's chat triggered them, not to a separate admin channel. There's no built-in "employee requests, only admin approves" — you'd have to build that routing yourself via gateway lifecycle hooks. Moot while terminal is disabled for everyone, but relevant if that ever changes.
- [ ] If a business genuinely needs an agent that runs commands, do it in a **separate, restricted profile that only your team reaches** — never expose command execution to the general employee-facing virtual employees.
- [ ] For any MCP integration you connect (Google Docs, Dropbox, etc.), remember **MCP tool calls have no approval gate today** — this is an open gap in Hermes itself, not something `approvals.*` covers. Mitigate by:
  - [ ] Using read-only or narrowly-scoped API credentials wherever the virtual employee's job is read/summarize rather than manage.
  - [ ] Restricting any delete/write-capable MCP tool to a separate, more locked-down profile — not exposed to the general employee-facing virtual employees.
  - [ ] Confirming the destination service's own recovery exists as a fallback (Google Drive trash/version history, Dropbox Rewind) in case something does get deleted.
  - [ ] Considering a custom gateway hook (pre-tool-call) that intercepts destructive-sounding MCP tool names and forces a manual approval step, since Hermes doesn't do this natively yet — optional, only worth it if you actually grant write/delete scope somewhere.
- [ ] Set `memory.write_approval: true` if you want every memory write staged for your review rather than saved freely — optional, adds friction but useful while you're still trusting the setup.

## Phase 5 — Admin access (you, not employees)

- [ ] No Hermes profile at this business gets terminal/execute_code access — including any profile you personally use to chat with the business's agents.
- [ ] All infra-level maintenance (installs, debugging, manual fixes) happens via Render's shell/SSH directly against the container — outside Hermes' chat/approval system entirely, and outside any per-employee authorization scheme.
- [ ] Document this access path (credentials, how you reach it) somewhere durable and separate from the business's own Hermes instance.

## Phase 6 — Backups and settings-as-code

- [ ] **Settings as code:** keep `config.yaml`, `SOUL.md`, and skills for each profile under version control. The cleanest vehicle for this is Hermes' own **Profile Distributions** feature — package each virtual employee's SOUL/config/skills as a git repo, which doubles as your template for repeating this setup at the next business (`hermes profile install github.com/you/template --alias`). Credentials, memories, and sessions stay per-install and are never pulled into the shared template repo.
- [ ] Set up a scheduled job (Render cron job, or a Hermes cron job with `cron_mode: deny` so it never silently approves anything dangerous) that commits any drift in config/SOUL/skills to that git repo automatically — this is your "auto-commit of settings."
- [ ] **Full data snapshots:** schedule `hermes profile export` (or `hermes backup`) on a recurring basis, shipping the export off the Render disk to external storage (S3 or similar) — this covers actual disaster recovery (memories, sessions, config) in a way git-tracked settings alone don't.
- [ ] Decide and document a retention window for these snapshots.
- [ ] If you self-host anything for a future business (Honcho self-hosted, OpenViking, etc.), make sure that service's own data is included in your backup scope too — it won't be covered by Hermes' own export.

## Phase 7 — Cross-virtual-employee collaboration (your Google Docs/Dropbox plan)

- [ ] Keep each virtual employee's memory fully separate (no shared memory store, no shared workspace-level bleed beyond what Honcho's peer model already isolates).
- [ ] Give virtual employees that need to collaborate shared access to specific documents/folders (Google Docs, Dropbox) rather than shared memory or shared sessions — this was your own instinct and it holds up: it's a clean, auditable bridge that doesn't depend on any of the murkier cross-session/cross-profile guarantees discussed above.
- [ ] Scope those document/folder credentials narrowly per the Phase 4 MCP guardrails — read-only where possible, and be deliberate about which virtual employees get write access to shared documents.

## Phase 8 — Before going live with real employees

- [ ] Confirm Phase 0's `session_search` test came back clean (or decide you're comfortable with the residual risk for this business's data sensitivity level).
- [ ] Confirm Render shell/SSH access actually works as your sole admin path.
- [ ] Confirm terminal/execute_code is genuinely inaccessible from every employee-facing profile (test it — have a test employee try to ask a virtual employee to run a shell command, and confirm it can't).
- [ ] Confirm Honcho's `userPeerAliases` mapping is correctly distinguishing your test employees (not collapsing them into one peer).
- [ ] Confirm the built-in USER.md is not accumulating blended per-employee facts underneath Honcho (see Phase 3) — this is the built-in-memory bug check.
- [ ] Confirm your settings-as-code repo and your data-snapshot job are both actually running on schedule, not just configured.
- [ ] Do one full restore drill from a data snapshot before you need it for real.
