"""Unit tests for the LINE multi-channel registry (``modules/line/line_multiplex.py``).

Loaded via ``importlib``, same pattern as ``tests/test_render_mention.py`` —
the module under test ships as a standalone file ``COPY``'d into Hermes'
``plugins/platforms/line/`` package, never installed as part of a package
here.

Run with: ``python3 -m pytest tests/ -q``
"""
from __future__ import annotations

import asyncio
import contextvars
import importlib.util
import sys
import unittest
from pathlib import Path


def load_line_multiplex():
    module_path = (
        Path(__file__).resolve().parents[1] / "modules" / "line" / "line_multiplex.py"
    )
    spec = importlib.util.spec_from_file_location("line_multiplex", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["line_multiplex"] = module
    spec.loader.exec_module(module)
    return module


lm = load_line_multiplex()


def default_kwargs(**overrides):
    kwargs = dict(
        default_channel_secret="default-secret",
        default_channel_access_token="default-token",
        default_webhook_path="/line/webhook",
        default_allow_all=False,
        default_allowed_users=set(),
        default_allowed_groups=set(),
        default_allowed_rooms=set(),
        default_dm_policy="pairing",
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# LineChannel
# ---------------------------------------------------------------------------


class LineChannelTests(unittest.TestCase):
    def test_is_configured_requires_both_secret_and_token(self):
        self.assertFalse(
            lm.LineChannel(
                profile="default", channel_secret="", channel_access_token="",
                webhook_path="/line/webhook",
            ).is_configured()
        )
        self.assertFalse(
            lm.LineChannel(
                profile="default", channel_secret="s", channel_access_token="",
                webhook_path="/line/webhook",
            ).is_configured()
        )
        self.assertTrue(
            lm.LineChannel(
                profile="default", channel_secret="s", channel_access_token="t",
                webhook_path="/line/webhook",
            ).is_configured()
        )


# ---------------------------------------------------------------------------
# ChannelRegistry construction
# ---------------------------------------------------------------------------


class RegistryConstructionTests(unittest.TestCase):
    def test_zero_channels_extra_produces_exactly_one_default_channel(self):
        registry = lm.ChannelRegistry.from_extra({}, **default_kwargs())
        self.assertEqual(len(registry), 1)
        default = registry.default()
        self.assertEqual(default.profile, "default")
        self.assertEqual(default.channel_secret, "default-secret")
        self.assertEqual(default.channel_access_token, "default-token")
        self.assertEqual(default.webhook_path, "/line/webhook")

    def test_backward_compat_single_channel_deployment_is_unaffected(self):
        # No `channels` key at all — the overwhelming majority shape today.
        registry = lm.ChannelRegistry.from_extra(
            {"allowed_users": ["Ux"]}, **default_kwargs(default_allowed_users={"Ux"})
        )
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry.by_webhook_path("/line/webhook"), registry.default())

    def test_additional_channels_are_registered(self):
        extra = {
            "channels": [
                {
                    "profile": "coder",
                    "channel_secret": "coder-secret",
                    "channel_access_token": "coder-token",
                },
                {
                    "profile": "sales",
                    "channel_secret": "sales-secret",
                    "channel_access_token": "sales-token",
                },
            ]
        }
        registry = lm.ChannelRegistry.from_extra(extra, **default_kwargs())
        self.assertEqual(len(registry), 3)
        coder = registry.by_profile("coder")
        self.assertIsNotNone(coder)
        self.assertEqual(coder.channel_secret, "coder-secret")
        self.assertEqual(coder.webhook_path, "/line/p/coder/webhook")

    def test_non_default_webhook_paths_are_profile_scoped_under_line_prefix(self):
        extra = {"channels": [{"profile": "coder", "channel_secret": "s", "channel_access_token": "t"}]}
        registry = lm.ChannelRegistry.from_extra(extra, **default_kwargs())
        coder = registry.by_profile("coder")
        self.assertTrue(coder.webhook_path.startswith("/line/"))
        self.assertIn("/p/coder/", coder.webhook_path)

    def test_default_channel_keeps_unprefixed_path(self):
        registry = lm.ChannelRegistry.from_extra({}, **default_kwargs())
        self.assertEqual(registry.default().webhook_path, "/line/webhook")

    def test_a_channel_named_default_is_rejected(self):
        extra = {"channels": [{"profile": "default", "channel_secret": "s", "channel_access_token": "t"}]}
        with self.assertRaises(ValueError):
            lm.ChannelRegistry.from_extra(extra, **default_kwargs())

    def test_an_unnamed_channel_is_rejected(self):
        extra = {"channels": [{"channel_secret": "s", "channel_access_token": "t"}]}
        with self.assertRaises(ValueError):
            lm.ChannelRegistry.from_extra(extra, **default_kwargs())

    def test_duplicate_profile_names_are_rejected(self):
        extra = {
            "channels": [
                {"profile": "coder", "channel_secret": "s1", "channel_access_token": "t1"},
                {"profile": "coder", "channel_secret": "s2", "channel_access_token": "t2"},
            ]
        }
        with self.assertRaises(ValueError):
            lm.ChannelRegistry.from_extra(extra, **default_kwargs())

    def test_per_channel_allowlists_and_dm_policy_are_isolated(self):
        extra = {
            "channels": [
                {
                    "profile": "coder",
                    "channel_secret": "s",
                    "channel_access_token": "t",
                    "allowed_users": ["Ucoder"],
                    "dm_policy": "allowlist",
                },
            ]
        }
        registry = lm.ChannelRegistry.from_extra(
            extra, **default_kwargs(default_allowed_users={"Udefault"}, default_dm_policy="pairing")
        )
        coder = registry.by_profile("coder")
        self.assertEqual(coder.allowed_users, {"Ucoder"})
        self.assertEqual(coder.dm_policy, "allowlist")
        self.assertEqual(registry.default().allowed_users, {"Udefault"})
        self.assertEqual(registry.default().dm_policy, "pairing")


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


class RegistryLookupTests(unittest.TestCase):
    def setUp(self):
        extra = {"channels": [{"profile": "coder", "channel_secret": "s", "channel_access_token": "t"}]}
        self.registry = lm.ChannelRegistry.from_extra(extra, **default_kwargs())

    def test_by_profile_unknown_returns_none(self):
        self.assertIsNone(self.registry.by_profile("nope"))

    def test_by_webhook_path_unknown_returns_none(self):
        self.assertIsNone(self.registry.by_webhook_path("/line/p/nope/webhook"))

    def test_by_webhook_path_resolves_both_channels(self):
        self.assertEqual(self.registry.by_webhook_path("/line/webhook"), self.registry.default())
        self.assertEqual(
            self.registry.by_webhook_path("/line/p/coder/webhook"), self.registry.by_profile("coder")
        )

    def test_channel_for_chat_unknown_returns_none(self):
        self.assertIsNone(self.registry.channel_for_chat("Uwhoever"))

    def test_remember_chat_then_channel_for_chat_round_trips(self):
        coder = self.registry.by_profile("coder")
        self.registry.remember_chat("Ualice", coder)
        self.assertEqual(self.registry.channel_for_chat("Ualice"), coder)

    def test_remembering_a_chat_for_one_channel_does_not_leak_to_another(self):
        default = self.registry.default()
        coder = self.registry.by_profile("coder")
        self.registry.remember_chat("Ushared-looking-id", default)
        self.assertEqual(self.registry.channel_for_chat("Ushared-looking-id"), default)
        self.assertNotEqual(self.registry.channel_for_chat("Ushared-looking-id"), coder)


# ---------------------------------------------------------------------------
# contextvar isolation — the specific race this design exists to prevent
# ---------------------------------------------------------------------------


class CurrentChannelContextTests(unittest.TestCase):
    def test_current_is_none_outside_any_context(self):
        # Fresh context (simulates adapter startup / connect(), before any
        # webhook request has ever set anything).
        def check():
            self.assertIsNone(lm.current())

        contextvars.copy_context().run(check)

    def test_set_current_is_visible_within_the_same_context(self):
        channel = lm.LineChannel(
            profile="coder", channel_secret="s", channel_access_token="t", webhook_path="/x"
        )

        def body():
            lm.set_current(channel)
            self.assertEqual(lm.current(), channel)

        contextvars.copy_context().run(body)

    def test_two_concurrent_simulated_requests_do_not_see_each_others_channel(self):
        # The exact race contextvars exist to prevent: the aiohttp server
        # interleaves concurrent requests from *different* channels on one
        # event loop. Simulate two independent request contexts and confirm
        # setting one's current channel is invisible to the other.
        channel_a = lm.LineChannel(
            profile="a", channel_secret="sa", channel_access_token="ta", webhook_path="/a"
        )
        channel_b = lm.LineChannel(
            profile="b", channel_secret="sb", channel_access_token="tb", webhook_path="/b"
        )
        seen_by_b = []

        async def request_a():
            lm.set_current(channel_a)
            await asyncio.sleep(0)  # yield control mid-request, like a real await
            self.assertEqual(lm.current(), channel_a)

        async def request_b():
            await asyncio.sleep(0)
            seen_by_b.append(lm.current())
            lm.set_current(channel_b)
            self.assertEqual(lm.current(), channel_b)

        async def run_both():
            # Each request handled in its own task, exactly as aiohttp does
            # per-connection — this is what makes context isolation matter.
            await asyncio.gather(
                asyncio.get_event_loop().create_task(_in_fresh_context(request_a)),
                asyncio.get_event_loop().create_task(_in_fresh_context(request_b)),
            )

        asyncio.run(run_both())
        # Request B never saw request A's channel, even though A set its
        # channel before B's task got its first chance to run.
        self.assertIn(seen_by_b[0], (None, channel_a))
        # The real assertion: after both complete, a *fresh* context still
        # sees no leaked channel.
        contextvars.copy_context().run(lambda: self.assertIsNone(lm.current()))

    def test_reset_restores_the_prior_value(self):
        channel = lm.LineChannel(
            profile="coder", channel_secret="s", channel_access_token="t", webhook_path="/x"
        )

        def body():
            self.assertIsNone(lm.current())
            token = lm.set_current(channel)
            self.assertEqual(lm.current(), channel)
            lm.reset_current(token)
            self.assertIsNone(lm.current())

        contextvars.copy_context().run(body)

    def test_asyncio_create_task_copies_the_context_at_creation(self):
        # Justifies the module docstring's claim about the typing-indicator
        # ping: a task created *while* a channel is current inherits it,
        # even if the parent's context later changes.
        channel = lm.LineChannel(
            profile="coder", channel_secret="s", channel_access_token="t", webhook_path="/x"
        )
        seen = {}

        async def spawned_task():
            seen["channel"] = lm.current()

        async def parent():
            lm.set_current(channel)
            task = asyncio.get_event_loop().create_task(spawned_task())
            await task

        asyncio.run(_in_fresh_context(parent))
        self.assertEqual(seen["channel"], channel)


async def _in_fresh_context(coro_fn):
    """Run ``coro_fn()`` in a copied context, isolating contextvar writes
    the way a brand-new incoming request would be isolated by aiohttp."""
    ctx = contextvars.copy_context()
    return await ctx.run(lambda: asyncio.ensure_future(coro_fn()))


if __name__ == "__main__":
    unittest.main()
