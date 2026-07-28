"""Unit tests for the LINE group mention gate (``modules/line/render_mention.py``).

Loaded via ``importlib`` rather than a package import, same as the repo's
previous ``tests/test_patch_config.py`` — the module under test ships as a
standalone file that is ``COPY``'d into Hermes' ``plugins/platforms/line/``
package, so it is never installed as part of a package here.

Run with: ``python3 -m pytest tests/ -q``
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import sys
import unittest
from pathlib import Path


def load_render_mention():
    module_path = (
        Path(__file__).resolve().parents[1] / "modules" / "line" / "render_mention.py"
    )
    spec = importlib.util.spec_from_file_location("render_mention", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_mention"] = module
    spec.loader.exec_module(module)
    return module


rm = load_render_mention()


def text_message(text, mentionees=None):
    """A LINE webhook ``event["message"]`` for a text message."""
    message = {"type": "text", "id": "1", "text": text}
    if mentionees is not None:
        message["mention"] = {"mentionees": mentionees}
    return message


def self_mentionee(index, length):
    return {"index": index, "length": length, "type": "user", "isSelf": True}


def other_mentionee(index, length):
    return {
        "index": index,
        "length": length,
        "type": "user",
        "userId": "Uother",
        "isSelf": False,
    }


class FakeClient:
    """Stand-in for ``_LineClient`` — the raw-message API the adapter uses.

    Matches upstream: template/button payloads go out through
    ``reply(reply_token, [message])`` or ``push(chat_id, [message])``, never
    through the text-oriented ``LineAdapter.send``.
    """

    def __init__(self):
        self.replied = []
        self.pushed = []

    async def reply(self, reply_token, messages):
        self.replied.append((reply_token, messages))

    async def push(self, chat_id, messages):
        self.pushed.append((chat_id, messages))

    @property
    def sent(self):
        return self.replied + self.pushed


# ---------------------------------------------------------------------------
# Mention detection
# ---------------------------------------------------------------------------


class MentionDetectionTests(unittest.TestCase):
    def test_self_mention_is_detected(self):
        msg = text_message("@Bot hello", [self_mentionee(0, 4)])
        self.assertTrue(rm.is_self_mentioned(msg))

    def test_mention_of_another_user_is_not_a_self_mention(self):
        msg = text_message("@Alice hello", [other_mentionee(0, 6)])
        self.assertFalse(rm.is_self_mentioned(msg))

    def test_mention_all_counts_as_a_self_mention(self):
        msg = text_message("@all standup", [{"index": 0, "length": 4, "type": "all"}])
        self.assertTrue(rm.is_self_mentioned(msg))

    def test_no_mention_key_at_all(self):
        self.assertFalse(rm.is_self_mentioned(text_message("just chatting")))

    def test_non_text_message_is_never_a_mention(self):
        self.assertFalse(rm.is_self_mentioned({"type": "sticker", "id": "9"}))

    def test_malformed_mention_does_not_raise(self):
        for bad in ("not-a-dict", 42, None, [], {"mentionees": "nope"}, {}):
            msg = {"type": "text", "text": "hi", "mention": bad}
            self.assertFalse(rm.is_self_mentioned(msg), bad)

    def test_mentionee_entries_that_are_not_dicts_are_skipped(self):
        msg = text_message("hi", ["garbage", None, self_mentionee(0, 2)])
        self.assertTrue(rm.is_self_mentioned(msg))


# ---------------------------------------------------------------------------
# Mention stripping
# ---------------------------------------------------------------------------


class MentionStrippingTests(unittest.TestCase):
    def test_leading_mention_is_removed_and_whitespace_collapsed(self):
        msg = text_message("@Bot  what is the total?", [self_mentionee(0, 4)])
        self.assertEqual(
            rm.strip_self_mentions(msg["text"], msg), "what is the total?"
        )

    def test_mid_sentence_mention_is_removed_in_place(self):
        text = "hey @Bot can you check"
        msg = text_message(text, [self_mentionee(4, 4)])
        self.assertEqual(rm.strip_self_mentions(text, msg), "hey can you check")

    def test_only_the_self_mention_is_removed(self):
        text = "@Bot ask @Alice about it"
        msg = text_message(text, [self_mentionee(0, 4), other_mentionee(9, 6)])
        self.assertEqual(rm.strip_self_mentions(text, msg), "ask @Alice about it")

    def test_multiple_self_mentions_are_all_removed(self):
        text = "@Bot hello @Bot"
        msg = text_message(text, [self_mentionee(0, 4), self_mentionee(11, 4)])
        self.assertEqual(rm.strip_self_mentions(text, msg), "hello")

    def test_emoji_before_mention_still_strips_correctly(self):
        # LINE's `index` is not guaranteed to agree with Python's code-point
        # indexing once astral-plane characters are in play. "🎉" is one code
        # point in Python but two UTF-16 code units, so a client counting
        # UTF-16 reports index 3 where Python would say index 2.
        text = "🎉 @Bot congrats"
        self.assertEqual(text.index("@Bot"), 2)
        for reported_index in (2, 3):
            msg = text_message(text, [self_mentionee(reported_index, 4)])
            self.assertEqual(
                rm.strip_self_mentions(text, msg),
                "🎉 congrats",
                f"failed for reported index {reported_index}",
            )

    def test_out_of_range_span_leaves_text_untouched(self):
        text = "@Bot hi"
        for span in ((99, 4), (0, 999), (-5, 4)):
            msg = text_message(text, [self_mentionee(*span)])
            self.assertEqual(rm.strip_self_mentions(text, msg), text, span)

    def test_span_not_pointing_at_a_mention_leaves_text_untouched(self):
        text = "no mention here at all"
        msg = text_message(text, [self_mentionee(3, 4)])
        self.assertEqual(rm.strip_self_mentions(text, msg), text)

    def test_mention_all_is_not_stripped(self):
        text = "@all please review"
        msg = text_message(text, [{"index": 0, "length": 4, "type": "all"}])
        self.assertEqual(rm.strip_self_mentions(text, msg), text)

    def test_empty_text_is_safe(self):
        self.assertEqual(rm.strip_self_mentions("", text_message("")), "")


# ---------------------------------------------------------------------------
# Mode store
# ---------------------------------------------------------------------------


class GroupModeStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "line-modes.json"
        self._env_before = os.environ.get("LINE_REQUIRE_MENTION")
        os.environ.pop("LINE_REQUIRE_MENTION", None)

    def tearDown(self):
        self._tmp.cleanup()
        if self._env_before is None:
            os.environ.pop("LINE_REQUIRE_MENTION", None)
        else:
            os.environ["LINE_REQUIRE_MENTION"] = self._env_before

    def test_unseen_group_defaults_to_mention_only(self):
        store = rm.GroupModeStore(self.path)
        self.assertEqual(store.get_mode("Cgroup"), rm.MODE_MENTION)

    def test_env_var_overrides_the_default_for_unseen_groups(self):
        store = rm.GroupModeStore(self.path)
        os.environ["LINE_REQUIRE_MENTION"] = "false"
        self.assertEqual(store.get_mode("Cgroup"), rm.MODE_ALWAYS)
        os.environ["LINE_REQUIRE_MENTION"] = "true"
        self.assertEqual(store.get_mode("Cgroup"), rm.MODE_MENTION)

    def test_set_then_get_round_trips(self):
        store = rm.GroupModeStore(self.path)
        store.set_mode("Cgroup", rm.MODE_ALWAYS, "Umanager")
        self.assertEqual(store.get_mode("Cgroup"), rm.MODE_ALWAYS)

    def test_stored_mode_beats_the_env_default(self):
        store = rm.GroupModeStore(self.path)
        store.set_mode("Cgroup", rm.MODE_ALWAYS, "Umanager")
        os.environ["LINE_REQUIRE_MENTION"] = "true"
        self.assertEqual(store.get_mode("Cgroup"), rm.MODE_ALWAYS)

    def test_modes_are_per_group(self):
        store = rm.GroupModeStore(self.path)
        store.set_mode("Cone", rm.MODE_ALWAYS, "U1")
        self.assertEqual(store.get_mode("Ctwo"), rm.MODE_MENTION)

    def test_set_mode_records_who_and_when(self):
        store = rm.GroupModeStore(self.path)
        store.set_mode("Cgroup", rm.MODE_ALWAYS, "Umanager")
        entry = json.loads(self.path.read_text())["Cgroup"]
        self.assertEqual(entry["mode"], rm.MODE_ALWAYS)
        self.assertEqual(entry["set_by"], "Umanager")
        self.assertIn("set_at", entry)

    def test_file_is_written_private(self):
        store = rm.GroupModeStore(self.path)
        store.set_mode("Cgroup", rm.MODE_ALWAYS, "U1")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_invalid_mode_is_rejected(self):
        store = rm.GroupModeStore(self.path)
        with self.assertRaises(ValueError):
            store.set_mode("Cgroup", "sometimes", "U1")

    def test_corrupt_file_falls_back_to_default(self):
        self.path.write_text("{not json at all")
        store = rm.GroupModeStore(self.path)
        self.assertEqual(store.get_mode("Cgroup"), rm.MODE_MENTION)

    def test_corrupt_file_is_overwritten_by_a_later_set(self):
        self.path.write_text("{not json at all")
        store = rm.GroupModeStore(self.path)
        store.set_mode("Cgroup", rm.MODE_ALWAYS, "U1")
        self.assertEqual(store.get_mode("Cgroup"), rm.MODE_ALWAYS)

    def test_unrecognised_stored_value_falls_back_to_default(self):
        self.path.write_text(json.dumps({"Cgroup": {"mode": "banana"}}))
        store = rm.GroupModeStore(self.path)
        self.assertEqual(store.get_mode("Cgroup"), rm.MODE_MENTION)

    def test_a_separate_instance_sees_the_write_without_reconstruction(self):
        # The whole point of the JSON store: no caching, so a flip takes
        # effect on the very next message with no gateway restart.
        reader = rm.GroupModeStore(self.path)
        self.assertEqual(reader.get_mode("Cgroup"), rm.MODE_MENTION)
        rm.GroupModeStore(self.path).set_mode("Cgroup", rm.MODE_ALWAYS, "U1")
        self.assertEqual(reader.get_mode("Cgroup"), rm.MODE_ALWAYS)


# ---------------------------------------------------------------------------
# Follow-up window
# ---------------------------------------------------------------------------


class FollowupWindowTests(unittest.TestCase):
    def test_mentioner_passes_inside_the_window(self):
        window = rm.FollowupWindow(90)
        window.record("Cgroup", "Ualice", now=1000.0)
        self.assertTrue(window.is_open("Cgroup", "Ualice", now=1050.0))

    def test_mentioner_fails_after_the_window(self):
        window = rm.FollowupWindow(90)
        window.record("Cgroup", "Ualice", now=1000.0)
        self.assertFalse(window.is_open("Cgroup", "Ualice", now=1100.0))

    def test_another_member_does_not_inherit_the_window(self):
        window = rm.FollowupWindow(90)
        window.record("Cgroup", "Ualice", now=1000.0)
        self.assertFalse(window.is_open("Cgroup", "Ubob", now=1010.0))

    def test_window_is_per_group(self):
        window = rm.FollowupWindow(90)
        window.record("Cone", "Ualice", now=1000.0)
        self.assertFalse(window.is_open("Ctwo", "Ualice", now=1010.0))

    def test_zero_seconds_disables_the_window(self):
        window = rm.FollowupWindow(0)
        window.record("Cgroup", "Ualice", now=1000.0)
        self.assertFalse(window.is_open("Cgroup", "Ualice", now=1000.5))

    def test_recording_again_extends_the_window(self):
        window = rm.FollowupWindow(90)
        window.record("Cgroup", "Ualice", now=1000.0)
        window.record("Cgroup", "Ualice", now=1080.0)
        self.assertTrue(window.is_open("Cgroup", "Ualice", now=1150.0))

    def test_a_new_mentioner_takes_over_the_window(self):
        window = rm.FollowupWindow(90)
        window.record("Cgroup", "Ualice", now=1000.0)
        window.record("Cgroup", "Ubob", now=1010.0)
        self.assertFalse(window.is_open("Cgroup", "Ualice", now=1020.0))
        self.assertTrue(window.is_open("Cgroup", "Ubob", now=1020.0))


# ---------------------------------------------------------------------------
# Command + postback parsing
# ---------------------------------------------------------------------------


class ModeCommandTests(unittest.TestCase):
    def test_bare_mode_shows_current_state(self):
        self.assertEqual(rm.parse_mode_command("mode"), rm.COMMAND_SHOW)

    def test_explicit_modes(self):
        self.assertEqual(rm.parse_mode_command("mode always"), rm.MODE_ALWAYS)
        self.assertEqual(rm.parse_mode_command("mode mention"), rm.MODE_MENTION)

    def test_is_case_and_whitespace_insensitive(self):
        self.assertEqual(rm.parse_mode_command("  MODE   Always "), rm.MODE_ALWAYS)

    def test_ordinary_chat_is_not_a_command(self):
        for text in ("", "what mode are we in?", "modest proposal", "mode banana"):
            self.assertIsNone(rm.parse_mode_command(text), text)


class ModePostbackTests(unittest.TestCase):
    def test_button_payload_round_trips_through_the_parser(self):
        payload = rm.build_mode_buttons_message("Cgroup", rm.MODE_MENTION)
        actions = payload["template"]["actions"]
        self.assertEqual(len(actions), 2)
        parsed = [rm.parse_mode_postback(json.loads(a["data"])) for a in actions]
        self.assertEqual(set(parsed), {rm.MODE_MENTION, rm.MODE_ALWAYS})

    def test_button_labels_respect_lines_twenty_char_limit(self):
        payload = rm.build_mode_buttons_message("Cgroup", rm.MODE_ALWAYS)
        for action in payload["template"]["actions"]:
            self.assertLessEqual(len(action["label"]), 20)

    def test_payload_text_respects_lines_limits(self):
        payload = rm.build_mode_buttons_message("Cgroup", rm.MODE_ALWAYS)
        self.assertLessEqual(len(payload["template"]["text"]), 160)
        self.assertLessEqual(len(payload["altText"]), 400)

    def test_buttons_set_an_absolute_mode_never_a_toggle(self):
        # A Template Buttons bubble stays tappable from history forever, so a
        # relative flip from a stale bubble would do the wrong thing.
        for current in (rm.MODE_MENTION, rm.MODE_ALWAYS):
            payload = rm.build_mode_buttons_message("Cgroup", current)
            modes = {
                json.loads(a["data"])["mode"] for a in payload["template"]["actions"]
            }
            self.assertEqual(modes, {rm.MODE_MENTION, rm.MODE_ALWAYS})

    def test_unrelated_postback_is_ignored(self):
        self.assertIsNone(
            rm.parse_mode_postback({"action": "show_response", "request_id": "r1"})
        )

    def test_malformed_postback_is_ignored(self):
        for bad in ({}, {"action": rm.POSTBACK_ACTION}, {"action": rm.POSTBACK_ACTION, "mode": "banana"}, "nope", None):
            self.assertIsNone(rm.parse_mode_postback(bad), bad)


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


class MentionGateTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "line-modes.json"
        self._env_before = os.environ.get("LINE_REQUIRE_MENTION")
        os.environ.pop("LINE_REQUIRE_MENTION", None)
        self.store = rm.GroupModeStore(path)
        self.gate = rm.MentionGate(store=self.store, followup_seconds=90)

    def tearDown(self):
        self._tmp.cleanup()
        if self._env_before is None:
            os.environ.pop("LINE_REQUIRE_MENTION", None)
        else:
            os.environ["LINE_REQUIRE_MENTION"] = self._env_before

    def evaluate(self, message, chat_type="group", user_id="Ualice", now=1000.0):
        return self.gate.evaluate(
            chat_type=chat_type,
            chat_id="Cgroup",
            user_id=user_id,
            message=message,
            now=now,
        )

    def test_dm_always_passes_without_a_mention(self):
        decision = self.evaluate(text_message("hello"), chat_type="dm")
        self.assertTrue(decision.allow)

    def test_dm_never_consults_the_store(self):
        self.store.set_mode("Cgroup", rm.MODE_MENTION, "U1")
        decision = self.evaluate(text_message("hello"), chat_type="dm")
        self.assertTrue(decision.allow)

    def test_group_message_without_a_mention_is_dropped(self):
        decision = self.evaluate(text_message("hey bob, lunch?"))
        self.assertFalse(decision.allow)

    def test_room_behaves_like_a_group(self):
        decision = self.evaluate(text_message("hey bob"), chat_type="room")
        self.assertFalse(decision.allow)

    def test_group_message_with_a_self_mention_passes(self):
        msg = text_message("@Bot what is the total?", [self_mentionee(0, 4)])
        decision = self.evaluate(msg)
        self.assertTrue(decision.allow)

    def test_mention_is_stripped_from_the_text_the_agent_sees(self):
        msg = text_message("@Bot what is the total?", [self_mentionee(0, 4)])
        decision = self.evaluate(msg)
        self.assertEqual(decision.stripped_text, "what is the total?")

    def test_always_mode_passes_everything(self):
        self.store.set_mode("Cgroup", rm.MODE_ALWAYS, "U1")
        self.assertTrue(self.evaluate(text_message("no mention here")).allow)

    def test_always_mode_takes_effect_without_rebuilding_the_gate(self):
        self.assertFalse(self.evaluate(text_message("hi")).allow)
        rm.GroupModeStore(self.store.path).set_mode("Cgroup", rm.MODE_ALWAYS, "U1")
        self.assertTrue(self.evaluate(text_message("hi")).allow)

    def test_mentioner_gets_a_free_followup_inside_the_window(self):
        msg = text_message("@Bot hi", [self_mentionee(0, 4)])
        self.assertTrue(self.evaluate(msg, now=1000.0).allow)
        self.assertTrue(self.evaluate(text_message("and also"), now=1030.0).allow)

    def test_followup_expires(self):
        msg = text_message("@Bot hi", [self_mentionee(0, 4)])
        self.evaluate(msg, now=1000.0)
        self.assertFalse(self.evaluate(text_message("and also"), now=1200.0).allow)

    def test_another_member_does_not_get_the_free_followup(self):
        msg = text_message("@Bot hi", [self_mentionee(0, 4)])
        self.evaluate(msg, now=1000.0)
        decision = self.evaluate(text_message("unrelated"), user_id="Ubob", now=1010.0)
        self.assertFalse(decision.allow)

    def test_sticker_from_the_mentioner_passes_inside_the_window(self):
        msg = text_message("@Bot look", [self_mentionee(0, 4)])
        self.evaluate(msg, now=1000.0)
        sticker = {"type": "sticker", "id": "9"}
        self.assertTrue(self.evaluate(sticker, now=1010.0).allow)

    def test_sticker_outside_the_window_is_dropped(self):
        sticker = {"type": "sticker", "id": "9"}
        self.assertFalse(self.evaluate(sticker).allow)

    def test_an_image_can_never_carry_a_mention_so_only_the_window_saves_it(self):
        image = {"type": "image", "id": "9"}
        self.assertFalse(self.evaluate(image, now=1000.0).allow)
        msg = text_message("@Bot look at this", [self_mentionee(0, 4)])
        self.evaluate(msg, now=1001.0)
        self.assertTrue(self.evaluate(image, now=1002.0).allow)

    def test_a_dropped_message_does_not_open_a_window(self):
        self.evaluate(text_message("no mention"), now=1000.0)
        self.assertFalse(self.evaluate(text_message("still none"), now=1001.0).allow)

    def test_always_mode_does_not_open_a_mention_window(self):
        # In always-mode everything passes anyway; the window must not leak
        # into mention-mode behavior if the group is flipped back.
        self.store.set_mode("Cgroup", rm.MODE_ALWAYS, "U1")
        self.evaluate(text_message("hi"), now=1000.0)
        self.store.set_mode("Cgroup", rm.MODE_MENTION, "U1")
        self.assertFalse(self.evaluate(text_message("hi"), now=1005.0).allow)

    def test_mode_command_is_surfaced_and_not_passed_to_the_agent(self):
        msg = text_message("@Bot mode always", [self_mentionee(0, 4)])
        decision = self.evaluate(msg)
        self.assertEqual(decision.command, rm.MODE_ALWAYS)
        self.assertFalse(decision.allow)

    def test_mode_command_works_without_a_mention_in_always_mode(self):
        self.store.set_mode("Cgroup", rm.MODE_ALWAYS, "U1")
        decision = self.evaluate(text_message("mode mention"))
        self.assertEqual(decision.command, rm.MODE_MENTION)

    def test_mode_command_is_ignored_in_a_dm(self):
        # DMs have no mode to set — the gate never applies there.
        decision = self.evaluate(text_message("mode always"), chat_type="dm")
        self.assertIsNone(decision.command)
        self.assertTrue(decision.allow)

    def test_unmentioned_mode_command_in_mention_mode_is_not_honoured(self):
        # Otherwise anyone could flip the gate off without addressing the bot.
        decision = self.evaluate(text_message("mode always"))
        self.assertIsNone(decision.command)
        self.assertFalse(decision.allow)


class MentionGateCommandHandlingTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "line-modes.json"
        self._env_before = os.environ.get("LINE_REQUIRE_MENTION")
        os.environ.pop("LINE_REQUIRE_MENTION", None)
        self.store = rm.GroupModeStore(path)
        self.gate = rm.MentionGate(store=self.store, followup_seconds=90)
        self.client = FakeClient()

    def tearDown(self):
        self._tmp.cleanup()
        if self._env_before is None:
            os.environ.pop("LINE_REQUIRE_MENTION", None)
        else:
            os.environ["LINE_REQUIRE_MENTION"] = self._env_before

    def test_command_sets_the_mode_and_replies_with_the_buttons(self):
        asyncio.run(
            self.gate.handle_command(
                self.client, rm.MODE_ALWAYS, "Cgroup", "Umanager", "token-1"
            )
        )
        self.assertEqual(self.store.get_mode("Cgroup"), rm.MODE_ALWAYS)
        self.assertEqual(len(self.client.replied), 1)
        token, messages = self.client.replied[0]
        self.assertEqual(token, "token-1")
        self.assertEqual(messages[0]["type"], "template")

    def test_the_card_reports_the_mode_just_set(self):
        asyncio.run(
            self.gate.handle_command(
                self.client, rm.MODE_ALWAYS, "Cgroup", "Umanager", "token-1"
            )
        )
        _, messages = self.client.replied[0]
        self.assertIn("Always reply", messages[0]["template"]["text"])

    def test_falls_back_to_push_without_a_reply_token(self):
        asyncio.run(
            self.gate.handle_command(self.client, rm.MODE_ALWAYS, "Cgroup", "Umanager")
        )
        self.assertEqual(self.client.replied, [])
        self.assertEqual(len(self.client.pushed), 1)
        self.assertEqual(self.client.pushed[0][0], "Cgroup")

    def test_show_command_reports_without_changing_anything(self):
        self.store.set_mode("Cgroup", rm.MODE_ALWAYS, "U1")
        asyncio.run(
            self.gate.handle_command(
                self.client, rm.COMMAND_SHOW, "Cgroup", "Umanager", "token-1"
            )
        )
        self.assertEqual(self.store.get_mode("Cgroup"), rm.MODE_ALWAYS)
        self.assertEqual(len(self.client.sent), 1)

    def test_postback_sets_the_mode(self):
        data = {"action": rm.POSTBACK_ACTION, "mode": rm.MODE_ALWAYS}
        handled = asyncio.run(
            self.gate.handle_postback(self.client, data, "Cgroup", "Umanager", "t")
        )
        self.assertTrue(handled)
        self.assertEqual(self.store.get_mode("Cgroup"), rm.MODE_ALWAYS)

    def test_unrelated_postback_is_not_handled(self):
        data = {"action": "show_response", "request_id": "r1"}
        handled = asyncio.run(
            self.gate.handle_postback(self.client, data, "Cgroup", "Umanager", "t")
        )
        self.assertFalse(handled)
        self.assertEqual(self.client.sent, [])

    def test_tapping_a_stale_button_twice_is_idempotent(self):
        data = {"action": rm.POSTBACK_ACTION, "mode": rm.MODE_ALWAYS}
        asyncio.run(self.gate.handle_postback(self.client, data, "Cgroup", "U1", "t"))
        asyncio.run(self.gate.handle_postback(self.client, data, "Cgroup", "U1", "t"))
        self.assertEqual(self.store.get_mode("Cgroup"), rm.MODE_ALWAYS)

    def test_a_send_failure_does_not_propagate(self):
        class BrokenClient:
            async def reply(self, reply_token, messages):
                raise RuntimeError("LINE API down")

            async def push(self, chat_id, messages):
                raise RuntimeError("LINE API down")

        asyncio.run(
            self.gate.handle_command(
                BrokenClient(), rm.MODE_ALWAYS, "Cgroup", "Umanager", "t"
            )
        )
        self.assertEqual(self.store.get_mode("Cgroup"), rm.MODE_ALWAYS)

    def test_a_missing_client_does_not_propagate(self):
        asyncio.run(
            self.gate.handle_command(None, rm.MODE_ALWAYS, "Cgroup", "Umanager", "t")
        )
        self.assertEqual(self.store.get_mode("Cgroup"), rm.MODE_ALWAYS)


if __name__ == "__main__":
    unittest.main()
