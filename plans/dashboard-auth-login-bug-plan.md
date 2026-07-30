# Dashboard Auth Login Bug — Investigation Plan

Goal: root-cause a live traceback in the Hermes dashboard's login flow,
found incidentally while investigating LINE DM pairing (see
`plans/line-dm-pairing-plan.md`) — unrelated to LINE/pairing, split out
here so it doesn't get lost or conflated with that work.

## Live-state findings (Render, 2026-07-21)

Confirmed via the Render MCP server against the actual `hermes` service
(`srv-d97k2t57vvec73ccpg2g`, workspace "All In GK's workspace"):

- A traceback appeared in logs at `2026-07-21T00:56:35`, originating in
  `hermes_cli/dashboard_auth/basic/__init__.py`'s `start_login`, reached
  via `hermes_cli/web_server.py`'s `_plugin_api_runtime_gate` →
  `_dashboard_auth_gate` → `dashboard_auth/middleware.py`'s
  `gated_auth_middleware` → `web_server.py`'s `auth_login` route
  handler. The full stack wasn't captured beyond these frames — only
  enough of the call chain to know it's a real exception inside the
  basic-auth login path, not a log line describing a handled failure.
- Unclear whether this was triggered by a real login attempt (wrong
  credentials, a malformed request) or something structural (e.g. a
  misconfigured `HERMES_DASHBOARD_BASIC_AUTH_*` var). Needs the full
  traceback and surrounding request context to tell.
- Also noticed, not yet investigated: the live service's `autoDeploy` is
  `"yes"` / `autoDeployTrigger: "commit"`, which contradicts the
  README's claim that "Render won't auto-deploy (the Blueprint sets
  `autoDeployTrigger: off`)." All deploys observed in `list_deploys`
  have `trigger: "manual"` or `"service_updated"` (none `"commit"`), so
  in practice deploys still appear to be manual today — but the
  service-level setting itself doesn't match what the Blueprint is
  documented to set. Worth confirming whether `render.yaml` actually
  still sets `autoDeployTrigger: off`, or whether this drifted via a
  manual change in the Render dashboard.

## Next steps

- [ ] Pull the full traceback (not just the frame list) from Render logs
  around `2026-07-21T00:56:35` — `list_logs` with a tight time window on
  `srv-d97k2t57vvec73ccpg2g` and `level: error` (the captured frames were
  logged at `info`, so also check whether the exception itself landed at
  a different level or in a different log line).
- [ ] Reproduce: attempt a dashboard login (correct and incorrect
  credentials) against the live service or a local smoke-test boot, and
  see whether either path reliably triggers the same traceback.
- [ ] Check `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` /
  `_PASSWORD` / `_SECRET` are all actually set (Render Environment tab)
  — `web_server.py`'s `_dashboard_auth_gate` refusing to bind without
  these is documented behavior in the README; a partially-set trio could
  plausibly surface as an exception in `start_login` rather than a clean
  refusal-to-bind, depending on where the check happens.
- [ ] Separately, confirm whether `render.yaml` in this repo still
  specifies `autoDeployTrigger: off`, and if so, why the live service
  shows `"yes"`/`"commit"` — reconcile the doc, the Blueprint, and the
  live setting so one of the three isn't just wrong.
- [ ] Once root-caused, decide whether this is a template-side
  misconfiguration (fixable in this repo) or an upstream Hermes bug
  (`hermes-agent`) worth reporting.
