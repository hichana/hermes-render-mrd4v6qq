# CLAUDE.md — patches/

Read @README.md in this directory before editing, generating, or reordering
anything here. The three things that get broken by not reading it:

1. **Order is load-bearing.** `line-group-mention.patch` is generated against a
   tree that *already* has `line-dm-pairing.patch` applied. Both touch
   `_handle_message_event` and `plugin.yaml`. Applied standalone against
   pristine upstream, the mention patch fails "patch does not apply". Apply and
   generate them in that order, always.

2. **Never hand-edit hunk headers or context lines** to make a patch apply.
   Regenerate the patch from a real tree at the pinned tag (README's
   "Regenerating a patch after bumping `HERMES_IMAGE`"). A hand-massaged patch
   that applies is not the same as one that's correct — `git apply` failing is
   the *safe* outcome, and forcing past it converts a build error into a
   silently wrong LINE adapter on a client's instance.

3. **These patches are pinned to a specific upstream tag.** They're generated
   against whatever `ARG HERMES_IMAGE` in the repo-root `Dockerfile` names. If
   that pin has moved and these files haven't been re-verified, treat every
   patch here as unverified — see `UPGRADING.md`, and start with
   `./scripts/upgrade-preflight.sh <tag>` rather than guessing.

`line-dm-pairing.tests.patch` is **not** applied by the `Dockerfile` (the
upstream image ships no `tests/`). Don't add a build step for it.
