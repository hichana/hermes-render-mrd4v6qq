# PACKAGING.md — what does and does not ship in the image

This repo mixes two kinds of files: things that ship inside the built
Docker image and run on a client's Render instance, and things that exist
only for admins working in this repo (docs, planning notes, per-client
service IDs). Getting that boundary wrong means a client-facing container
ends up carrying internal docs, client-to-service-ID mappings, or
CTO/CEO names baked into a layer anyone with container access can read.

## The core guarantee: explicit COPY only

The `Dockerfile` never does `COPY . .`. Every file that ends up in the
image is named individually:

```
COPY patches/line-dm-pairing.patch /tmp/line-dm-pairing.patch
COPY skills /opt/render-tools/skills-local
COPY scripts/patch-config.py /opt/render-tools/patch-config.py
COPY scripts/seed-env-from-render.py /opt/render-tools/seed-env-from-render.py
COPY scripts/bootstrap.sh /etc/cont-init.d/03-render-tools
COPY --from=caddy /usr/bin/caddy /tmp/caddy.orig
COPY --chown=root:root caddy/Caddyfile /etc/caddy/Caddyfile
COPY --chown=root:root caddy/s6-rc.d/caddy /etc/s6-overlay/s6-rc.d/caddy
```

This means packaging is opt-in by construction: a new file in the repo
root or a new subdirectory is invisible to the image unless someone adds
a `COPY` line for it. **This is the primary control.** The `.dockerignore`
below is defense-in-depth for the day someone adds a broad `COPY .` out
of habit — it is not the thing doing the work today.

## Never packaged (admin/repo-only)

These must never appear in a `COPY` line, and exist only for people
working in this repo:

| Path | Why it's sensitive |
|---|---|
| `CLAUDE.md` | Internal instructions, company/personnel names (NGraph, Singo Takahashi, Matt Chana), SSH debugging procedures, incident history |
| `SERVICES.md` | Per-client Render service IDs — a mapping that only makes sense to an admin holding account access |
| `README.md` | Admin-facing setup/upgrade instructions; not something a client needs inside their own container |
| `PACKAGING.md` (this file) | Meta-documentation about the packaging process itself |
| `plans/`, `whiteboards/` | Working notes / design scratch space |
| `.git/` | Full commit history, including anything ever committed and later removed |
| `.claude/` | Local agent config |
| `render.yaml` | Describes Render infra topology across all client services — not something one client's container should carry a copy of |
| `.env.example`, real `.env` files | `.env.example` is a template for admins provisioning a new instance; real `.env` is per-instance secrets, never committed and never baked into a layer |

## Packaged (client-facing, ships in the image)

| Path | Purpose |
|---|---|
| `patches/*.patch` | Applied to upstream Hermes source at build time |
| `skills/` | Local skill overlay (Pattern 3) |
| `scripts/patch-config.py`, `scripts/seed-env-from-render.py` | Boot-time idempotent config mutation (Pattern 1) |
| `scripts/bootstrap.sh` | cont-init hook wiring (Pattern 2) — installed as `/etc/cont-init.d/03-render-tools` |
| `caddy/` | Reverse proxy config and s6 service definition |

`scripts/smoke-test.sh` is **not** packaged — it drives the image from
the outside (`docker build` / `docker run`), it doesn't need to run
inside it.

## Defense-in-depth: `.dockerignore`

Even though nothing today relies on it, a `.dockerignore` should exist so
that a future `COPY .` (added without reading this file) fails safe
rather than silently shipping docs and client mappings into a live
container. It should exclude everything in the "never packaged" table
above, plus standard VCS/OS/editor noise.

## Verification: assert it at build time, not just by convention

Per Pattern 4 in `CLAUDE.md` ("assert real state, not log lines"),
`scripts/smoke-test.sh` should check the built image directly for the
files that must never be present, e.g.:

```sh
for f in CLAUDE.md SERVICES.md README.md PACKAGING.md render.yaml; do
  docker exec "${CONTAINER}" sh -c "[ ! -e /$f ] && [ ! -e /opt/hermes/$f ]" \
    || fail "${f} was found in the built image — packaging boundary broken"
done
```

Run the smoke test before every deploy image bump, same as the other
boot assertions — a packaging regression is exactly the kind of thing
that looks fine in `docker logs` and is only visible by inspecting the
actual filesystem.

## Checklist when adding a new file to the repo root or a new top-level directory

1. Does a client-facing container need this file to function? If no,
   stop — do not add a `COPY` line, and add the path to `.dockerignore`.
2. If yes, add an explicit `COPY <path> <dest>` line naming exactly that
   file or directory — never widen an existing `COPY` to a parent
   directory that also contains admin-only files.
3. Add the path to the "packaged" table above; if it's sensitive-adjacent
   (e.g. lives next to secrets), double check `.dockerignore` still
   excludes the things it shouldn't ship.
4. Run `./scripts/smoke-test.sh` and confirm the new packaging-boundary
   assertions still pass.
