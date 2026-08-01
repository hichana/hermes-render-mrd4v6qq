"""Multi-channel LINE credential/routing registry (hermes-render overlay).

Ships as ``plugins/platforms/line/line_multiplex.py`` inside the image — a
plain ``COPY`` into an existing package directory, so **no patch touches
this file**. ``patches/line-multi-channel.patch`` only adds the call-outs
in ``adapter.py`` that reach in here. Same split as
``modules/line/render_mention.py`` and for the same reason: upstream drift
can only break the thin adapter hunk, loudly, at ``docker build`` — and
everything below is unit-testable with plain pytest, no upstream clone
(``tests/test_line_multiplex.py``).

Why this exists: ``gateway.multiplex_profiles`` lets one gateway process
serve several agent profiles, but LINE (like every port-binding platform —
see ``SecondaryPortBindingConfigError`` in ``gateway/run.py``) is
architecturally single-instance, single-credential — it can only ever be
configured on the *default* profile, once. This module lets that one
``LineAdapter`` instance hold several *channels* (distinct LINE Developers
Console bot registrations, each with its own secret/token/allowlists), and
route each inbound webhook and outbound send to the right one — without
needing a second Hermes gateway process per client agent, which
``research/07-line-multi-channel-multiplexing.md`` ruled out on memory
grounds (~262 MB RSS per idle gateway process against this deployment's
2 GiB container ceiling).

Two distinct routing problems, two distinct mechanisms — read this before
changing either:

* **Inbound** (a webhook POST arrives): synchronous, single-request,
  single-event-loop-turn resolution. A ``contextvars.ContextVar`` is
  correct here because the aiohttp server interleaves concurrent requests
  from *different* channels on one event loop, and ``asyncio.create_task()``
  (used for the typing-indicator ping) copies the context automatically.
* **Outbound** (``LineAdapter.send(chat_id, content)`` is called): NOT
  reliably inside the inbound request's context. ``BasePlatformAdapter.
  handle_message()`` is documented upstream as returning quickly by
  spawning a background task — the actual reply can be sent seconds later,
  on a task this module never observed being created, so nothing guarantees
  the inbound contextvar is still (or ever was, on that task) set. Route
  outbound sends through ``ChannelRegistry.channel_for_chat()`` instead — a
  plain ``chat_id -> LineChannel`` map recorded at inbound-dispatch time,
  looked up by ``chat_id`` alone. This is safe *because* LINE's own
  ``userId``/``groupId``/``roomId`` values are channel-scoped opaque
  hashes — the same real person messaging two different channels gets two
  different IDs from LINE itself, so a ``chat_id`` collision across two of
  our configured channels isn't a practical risk, only a theoretical one at
  the same order of magnitude as a SHA-based ID collision.

Known v1 limitation, deliberately accepted rather than solved here: a
proactive send to a ``chat_id`` never previously seen inbound since the
last gateway restart has no channel to resolve to, and ``send()`` fails
with ``SendResult(success=False, ...)``. Solving this generally means
persisting the chat_id map to disk, which is more machinery than this
patch takes on. Note this is distinct from — and does not affect — LINE's
existing ``deliver=line`` cron-notification path: that goes through
``_standalone_send``/``LINE_HOME_CHANNEL`` in ``adapter.py``, a separate,
out-of-process mechanism this patch does not touch. It only ever sends via
the default channel's token today, patched or not — a secondary profile
has no ``platforms.line`` config to standalone-send through in the first
place (``SecondaryPortBindingConfigError``), so this isn't a regression.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class LineChannel:
    """One configured LINE Developers Console channel (bot registration).

    ``client``, ``bot_user_id`` and ``lock_key`` are runtime state, not
    config — ``None`` until ``LineAdapter.connect()`` populates them for
    this specific channel. Everything else is fixed at construction from
    ``config.yaml``/env.
    """

    profile: str
    channel_secret: str
    channel_access_token: str
    webhook_path: str
    allow_all: bool = False
    allowed_users: Set[str] = field(default_factory=set)
    allowed_groups: Set[str] = field(default_factory=set)
    allowed_rooms: Set[str] = field(default_factory=set)
    dm_policy: str = "pairing"

    # Runtime state, set by LineAdapter.connect()/disconnect().
    client: Any = None
    bot_user_id: Optional[str] = None
    lock_key: Optional[str] = None

    def is_configured(self) -> bool:
        return bool(self.channel_secret) and bool(self.channel_access_token)


DEFAULT_PROFILE = "default"


def _webhook_path_for(profile: str, base_path: str) -> str:
    """The route a non-default channel's webhook is served on.

    The default channel keeps today's unprefixed path unchanged (zero
    config change for every already-live single-channel client instance's
    LINE console callback URL). Every other channel gets a
    profile-scoped route.

    Deliberately **not** ``/p/{profile}/line/webhook`` — that's the URL
    convention Hermes' shared ``webhook``/``api_server`` listener uses for
    its own multi-tenant routing, and LINE has never participated in that
    listener; it's always bound its own dedicated port
    (``LINE_PORT``) behind this repo's own ``caddy/Caddyfile``, which only
    proxies ``handle /line/* { ... }`` (path-preserving, not
    ``handle_path``) to that port. A route under ``/p/*`` would not match
    that rule and would silently fall through to the dashboard instead of
    the LINE backend. ``/line/p/{profile}/webhook`` stays under the
    existing rule with zero Caddyfile changes.
    """
    if profile == DEFAULT_PROFILE:
        return base_path
    prefix, _, rest = base_path.partition("/line/")
    if not prefix and rest:
        return f"/line/p/{profile}/{rest}"
    # base_path didn't start with "/line/" (a custom webhook_path override)
    # — still namespace it by profile rather than silently colliding.
    trimmed = base_path.lstrip("/")
    return f"/line/p/{profile}/{trimmed}"


class ChannelRegistry:
    """Every LINE channel this adapter instance serves, plus lookup indexes.

    ``from_extra`` is the only constructor meant for adapter use — it always
    produces at least one channel (the default, built from the adapter's
    existing top-level credentials/allowlists/dm_policy) so a deployment
    with no ``channels`` configured behaves byte-identical to the
    single-channel adapter, unchanged.
    """

    def __init__(self, channels: List[LineChannel]) -> None:
        if not channels:
            raise ValueError("ChannelRegistry requires at least one channel")
        seen_profiles: Set[str] = set()
        seen_paths: Set[str] = set()
        for ch in channels:
            if ch.profile in seen_profiles:
                raise ValueError(f"duplicate LINE channel profile {ch.profile!r}")
            if ch.webhook_path in seen_paths:
                raise ValueError(
                    f"duplicate LINE webhook path {ch.webhook_path!r} "
                    f"(profile {ch.profile!r})"
                )
            seen_profiles.add(ch.profile)
            seen_paths.add(ch.webhook_path)

        self._by_profile: Dict[str, LineChannel] = {ch.profile: ch for ch in channels}
        self._by_path: Dict[str, LineChannel] = {ch.webhook_path: ch for ch in channels}
        self._by_chat_id: Dict[str, LineChannel] = {}

    @classmethod
    def from_extra(
        cls,
        extra: Dict[str, Any],
        *,
        default_channel_secret: str,
        default_channel_access_token: str,
        default_webhook_path: str,
        default_allow_all: bool,
        default_allowed_users: Set[str],
        default_allowed_groups: Set[str],
        default_allowed_rooms: Set[str],
        default_dm_policy: str,
    ) -> "ChannelRegistry":
        """Build a registry from ``platforms.line.extra`` plus the adapter's
        own already-resolved default-channel fields (env + top-level extra).

        The default channel is always present, always first, and is built
        from the adapter's existing single-channel resolution — this
        function only adds to it, never replaces it, so ``channels`` being
        entirely absent from ``extra`` (every currently-live instance)
        produces exactly one channel with exactly today's behavior.
        """
        channels = [
            LineChannel(
                profile=DEFAULT_PROFILE,
                channel_secret=default_channel_secret,
                channel_access_token=default_channel_access_token,
                webhook_path=default_webhook_path,
                allow_all=default_allow_all,
                allowed_users=set(default_allowed_users),
                allowed_groups=set(default_allowed_groups),
                allowed_rooms=set(default_allowed_rooms),
                dm_policy=default_dm_policy,
            )
        ]

        for raw in extra.get("channels") or []:
            if not isinstance(raw, dict):
                continue
            profile = str(raw.get("profile", "")).strip()
            if not profile or profile == DEFAULT_PROFILE:
                raise ValueError(
                    f"platforms.line.extra.channels entry has invalid "
                    f"profile {profile!r} — must be non-empty and not "
                    f"{DEFAULT_PROFILE!r} (that's the default channel, "
                    f"configured via the adapter's top-level fields)"
                )
            channels.append(
                LineChannel(
                    profile=profile,
                    channel_secret=str(raw.get("channel_secret", "")),
                    channel_access_token=str(raw.get("channel_access_token", "")),
                    webhook_path=_webhook_path_for(profile, default_webhook_path),
                    allow_all=bool(raw.get("allow_all_users", False)),
                    allowed_users=set(raw.get("allowed_users", [])),
                    allowed_groups=set(raw.get("allowed_groups", [])),
                    allowed_rooms=set(raw.get("allowed_rooms", [])),
                    dm_policy=str(raw.get("dm_policy", "pairing")).strip().lower(),
                )
            )

        return cls(channels)

    def __len__(self) -> int:
        return len(self._by_profile)

    def all(self) -> List[LineChannel]:
        return list(self._by_profile.values())

    def default(self) -> LineChannel:
        return self._by_profile[DEFAULT_PROFILE]

    def by_profile(self, profile: str) -> Optional[LineChannel]:
        return self._by_profile.get(profile)

    def by_webhook_path(self, path: str) -> Optional[LineChannel]:
        return self._by_path.get(path)

    def remember_chat(self, chat_id: str, channel: LineChannel) -> None:
        """Record that ``chat_id`` belongs to ``channel``, for later outbound
        ``channel_for_chat`` lookups. Called at inbound-dispatch time, while
        the channel is still known unambiguously from the webhook route."""
        if chat_id:
            self._by_chat_id[chat_id] = channel

    def channel_for_chat(self, chat_id: str) -> Optional[LineChannel]:
        """The channel a previously-observed ``chat_id`` belongs to, for
        outbound sends. ``None`` means this registry has never seen this
        chat_id — see the module docstring's "Known v1 limitation" for
        when that happens."""
        return self._by_chat_id.get(chat_id)


# ---------------------------------------------------------------------------
# Inbound, request-scoped "current channel" — see the module docstring for
# why this is deliberately NOT used for outbound sends.
# ---------------------------------------------------------------------------

_current_channel: "contextvars.ContextVar[Optional[LineChannel]]" = contextvars.ContextVar(
    "line_multiplex_current_channel", default=None
)


def set_current(channel: LineChannel) -> contextvars.Token:
    """Set the current request's channel. Returns a token for ``reset()``."""
    return _current_channel.set(channel)


def reset_current(token: contextvars.Token) -> None:
    _current_channel.reset(token)


def current() -> Optional[LineChannel]:
    """The channel handling the request on this call stack, or ``None``
    outside any request context (e.g. during ``connect()``)."""
    return _current_channel.get()
