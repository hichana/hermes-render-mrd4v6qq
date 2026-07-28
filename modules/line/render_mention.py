"""Group mention-gating for the LINE adapter (hermes-render overlay).

Ships as ``plugins/platforms/line/render_mention.py`` inside the image — a
plain ``COPY`` into an existing package directory, so **no patch touches this
file**. ``patches/line-group-mention.patch`` only adds the call-outs in
``adapter.py`` that reach in here.

Why the split: upstream Hermes ships ~180 commits a week and this repo
re-applies its adapter patches by hand on every ``HERMES_IMAGE`` bump (see
``patches/README.md``). Keeping the logic in a module we own means upstream
drift can only break the ~20-line patch — loudly, at ``docker build`` — and
means everything below is unit-testable in this repo with plain pytest, with
no upstream clone (``tests/test_render_mention.py``).

Behavior:

* **DMs are never gated.** The gate only applies to ``group``/``room`` chats.
* In ``mention`` mode a group message is processed only if it @-mentions this
  bot (LINE's own ``mention.mentionees[].isSelf``), or if it comes from
  whoever last mentioned the bot within the follow-up window. That window is
  also the only way a sticker/image/voice note is ever processed, since LINE
  attaches ``mention`` to text messages only.
* In ``always`` mode every group message is processed, like a DM.

Mode is stored per-group in a JSON file that is **re-read on every message**,
mirroring ``gateway.pairing.PairingStore.is_approved()``. That is deliberate:
per ``CLAUDE.md``, anything cached at adapter construction needs a verified
gateway restart to change, which is the wrong cost for a toggle people flip
from inside a chat.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

MODE_MENTION = "mention"
MODE_ALWAYS = "always"
VALID_MODES = frozenset({MODE_MENTION, MODE_ALWAYS})

COMMAND_SHOW = "show"
COMMAND_KEYWORD = "mode"

GROUP_CHAT_TYPES = frozenset({"group", "room"})

POSTBACK_ACTION = "set_line_mode"

DEFAULT_FOLLOWUP_SECONDS = 90.0

_MODE_LABELS = {
    MODE_MENTION: "Only when mentioned",
    MODE_ALWAYS: "Always reply",
}

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# Mention detection and stripping
# ---------------------------------------------------------------------------


def _mentionees(message: Any) -> list:
    """Every well-formed mentionee on a text message; ``[]`` for anything else.

    Tolerates every malformed shape seen in the wild (and then some) because
    this runs on unvalidated webhook input — a raise here would take down the
    whole event dispatch.
    """
    if not isinstance(message, dict) or message.get("type") != "text":
        return []
    mention = message.get("mention")
    if not isinstance(mention, dict):
        return []
    mentionees = mention.get("mentionees")
    if not isinstance(mentionees, list):
        return []
    return [m for m in mentionees if isinstance(m, dict)]


def is_self_mentioned(message: Any) -> bool:
    """Whether this bot was @-mentioned.

    ``isSelf`` is LINE's own signal that the mentionee is the account that
    received the webhook — no regex, no display-name matching, no comparison
    against our own user ID. ``@all`` counts too: it addresses everyone in the
    room, which includes us.
    """
    for mentionee in _mentionees(message):
        if mentionee.get("type") == "all":
            return True
        if mentionee.get("isSelf") is True:
            return True
    return False


def _utf16_to_codepoint_index(text: str, utf16_index: int) -> Optional[int]:
    """Translate a UTF-16 code-unit offset into a Python string index."""
    offset = 0
    for position, char in enumerate(text):
        if offset == utf16_index:
            return position
        offset += 2 if ord(char) > 0xFFFF else 1
    return len(text) if offset == utf16_index else None


def _resolve_span(text: str, index: Any, length: Any) -> Optional[Tuple[int, int]]:
    """Locate a mention in ``text``, tolerating UTF-16 vs code-point indexing.

    LINE reports ``index``/``length`` in UTF-16 code units, which only diverges
    from Python's code-point indexing once an astral-plane character (emoji) is
    involved. Rather than guess which convention a given client used, try the
    literal interpretation first and accept it only if it actually lands on an
    ``@`` — then fall back to the UTF-16 reading. If neither points at a
    mention, return ``None`` and leave the text alone: showing the agent a
    stray ``@Bot`` is a cosmetic problem, mangling the user's message is not.
    """
    if not isinstance(index, int) or not isinstance(length, int):
        return None
    if isinstance(index, bool) or isinstance(length, bool):
        return None
    if index < 0 or length <= 0:
        return None

    candidates = [(index, index + length)]
    start = _utf16_to_codepoint_index(text, index)
    if start is not None:
        end = _utf16_to_codepoint_index(text, index + length)
        candidates.append((start, end if end is not None else start + length))

    for begin, finish in candidates:
        if 0 <= begin < finish <= len(text) and text[begin] == "@":
            return begin, finish
    return None


def strip_self_mentions(text: str, message: Any) -> str:
    """Remove this bot's ``@mention`` from ``text``, leaving other mentions be.

    Mentions of *other* people are left in place — they're meaningful content
    the agent should see. ``@all`` is left in place for the same reason.
    """
    if not text:
        return text

    spans = []
    for mentionee in _mentionees(message):
        if mentionee.get("isSelf") is not True:
            continue
        span = _resolve_span(text, mentionee.get("index"), mentionee.get("length"))
        if span is not None:
            spans.append(span)

    if not spans:
        return text

    result = text
    for begin, finish in sorted(spans, reverse=True):
        result = result[:begin] + result[finish:]
    return re.sub(r"[ \t]{2,}", " ", result).strip()


# ---------------------------------------------------------------------------
# Mode command / postback parsing
# ---------------------------------------------------------------------------


def parse_mode_command(text: Any) -> Optional[str]:
    """Parse a mode command out of (already mention-stripped) message text.

    Returns ``COMMAND_SHOW``, ``MODE_MENTION``, ``MODE_ALWAYS``, or ``None``
    for anything that isn't a command — which is the overwhelming majority of
    messages, so this must never false-positive on ordinary chat.
    """
    if not isinstance(text, str):
        return None
    parts = text.strip().lower().split()
    if not parts or parts[0] != COMMAND_KEYWORD:
        return None
    if len(parts) == 1:
        return COMMAND_SHOW
    if len(parts) == 2 and parts[1] in VALID_MODES:
        return parts[1]
    return None


def parse_mode_postback(data: Any) -> Optional[str]:
    """Extract an absolute mode from a tapped mode button, else ``None``."""
    if not isinstance(data, dict) or data.get("action") != POSTBACK_ACTION:
        return None
    mode = data.get("mode")
    return mode if mode in VALID_MODES else None


def build_mode_buttons_message(chat_id: str, current_mode: str) -> Dict[str, Any]:
    """A Template Buttons bubble for switching this group's response mode.

    Template Buttons rather than Quick Reply chips: chips are dismissed the
    moment anyone sends the next message, which makes them useless as a
    control surface (upstream's own ``build_postback_button_message`` docstring
    makes the same point). Both buttons are always offered and each sets an
    **absolute** mode — a bubble stays tappable from history forever, so a
    relative "toggle" would do the wrong thing whenever an old one is tapped.
    """
    current_label = _MODE_LABELS.get(current_mode, current_mode)
    text = f"Response mode in this chat: {current_label}."
    return {
        "type": "template",
        "altText": text[:400],
        "template": {
            "type": "buttons",
            "text": text[:160],
            "actions": [
                {
                    "type": "postback",
                    "label": _MODE_LABELS[mode][:20],
                    "data": json.dumps(
                        {
                            "action": POSTBACK_ACTION,
                            "mode": mode,
                            "chat_id": chat_id,
                        }
                    ),
                    "displayText": f"Response mode: {_MODE_LABELS[mode]}",
                }
                for mode in (MODE_MENTION, MODE_ALWAYS)
            ],
        },
    }


# ---------------------------------------------------------------------------
# Per-group mode store
# ---------------------------------------------------------------------------


def _default_store_path() -> Path:
    try:
        from hermes_constants import get_hermes_dir  # type: ignore

        return Path(get_hermes_dir("platforms/line-modes", "line-modes")) / "modes.json"
    except Exception:
        home = os.environ.get("HERMES_HOME") or "/opt/data"
        return Path(home) / "platforms" / "line-modes" / "modes.json"


def default_mode() -> str:
    """The mode a group falls back to when the store has no entry for it.

    Read from the environment on every call rather than cached, so it behaves
    consistently with the store itself.
    """
    raw = (os.environ.get("LINE_REQUIRE_MENTION") or "").strip().lower()
    if raw in _FALSY:
        return MODE_ALWAYS
    return MODE_MENTION


class GroupModeStore:
    """Per-group response mode, persisted as JSON.

    ``get_mode`` re-reads the file on **every** call — no memoization, by
    design. That is what lets a toggle take effect on the very next message
    with no gateway restart, the same property ``PairingStore.is_approved()``
    relies on. Writes are atomic (temp file + ``os.replace``, chmod 0600),
    mirroring ``LineInviteStore`` in ``patches/line-dm-pairing.patch``.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else _default_store_path()
        self._lock = threading.RLock()

    def _load(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, indent=2, ensure_ascii=False))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def get_mode(self, chat_id: str) -> str:
        entry = self._load().get(chat_id)
        if isinstance(entry, dict) and entry.get("mode") in VALID_MODES:
            return entry["mode"]
        return default_mode()

    def set_mode(self, chat_id: str, mode: str, set_by: str = "") -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {sorted(VALID_MODES)}")
        with self._lock:
            data = self._load()
            data[chat_id] = {
                "mode": mode,
                "set_by": set_by,
                "set_at": time.time(),
            }
            self._save(data)


# ---------------------------------------------------------------------------
# Follow-up window
# ---------------------------------------------------------------------------


class FollowupWindow:
    """Short grace period after the bot answers a mention, scoped to one user.

    Deliberately in-memory and not persisted: a ~90s window is *correct* to
    lose on restart, and persisting it would mean disk writes on every group
    message. Scoped to the last mentioner rather than the whole room so that
    unrelated chatter between other members can't trigger the agent.
    """

    def __init__(self, seconds: float = DEFAULT_FOLLOWUP_SECONDS) -> None:
        self.seconds = float(seconds)
        self._open: Dict[str, Tuple[str, float]] = {}

    def record(self, chat_id: str, user_id: str, now: Optional[float] = None) -> None:
        if self.seconds <= 0:
            return
        now = time.time() if now is None else now
        self._open[chat_id] = (user_id, now + self.seconds)

    def is_open(self, chat_id: str, user_id: str, now: Optional[float] = None) -> bool:
        if self.seconds <= 0:
            return False
        entry = self._open.get(chat_id)
        if not entry:
            return False
        holder, expiry = entry
        now = time.time() if now is None else now
        if now >= expiry:
            self._open.pop(chat_id, None)
            return False
        return holder == user_id


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass
class GateDecision:
    """What the adapter should do with one inbound message."""

    allow: bool
    reason: str = ""
    stripped_text: Optional[str] = None
    command: Optional[str] = None


def _followup_seconds_from_env() -> float:
    raw = (os.environ.get("LINE_MENTION_FOLLOWUP_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_FOLLOWUP_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning(
            "LINE: ignoring invalid LINE_MENTION_FOLLOWUP_SECONDS=%r; using %s",
            raw, DEFAULT_FOLLOWUP_SECONDS,
        )
        return DEFAULT_FOLLOWUP_SECONDS


class MentionGate:
    """Decides whether a LINE message reaches the agent, and handles the toggle.

    Pragmatic (not semantic) by design: ``evaluate`` reads the store, consults
    the follow-up window **and refreshes it** as a side effect, so the adapter
    call site stays a handful of lines. The pure pieces it composes
    (``is_self_mentioned``, ``strip_self_mentions``, ``parse_mode_command``)
    are separately testable.
    """

    def __init__(
        self,
        store: Optional[GroupModeStore] = None,
        window: Optional[FollowupWindow] = None,
        followup_seconds: Optional[float] = None,
    ) -> None:
        self.store = store if store is not None else GroupModeStore()
        if window is not None:
            self.window = window
        else:
            seconds = (
                _followup_seconds_from_env()
                if followup_seconds is None
                else float(followup_seconds)
            )
            self.window = FollowupWindow(seconds)

    def evaluate(
        self,
        chat_type: str,
        chat_id: str,
        user_id: str,
        message: Any,
        now: Optional[float] = None,
    ) -> GateDecision:
        if chat_type not in GROUP_CHAT_TYPES:
            return GateDecision(allow=True, reason="dm")

        now = time.time() if now is None else now
        mode = self.store.get_mode(chat_id)
        is_text = isinstance(message, dict) and message.get("type") == "text"
        raw_text = message.get("text", "") if is_text else ""

        if mode == MODE_ALWAYS:
            stripped = strip_self_mentions(raw_text, message) if is_text else None
            command = parse_mode_command(stripped) if is_text else None
            if command is not None:
                return GateDecision(allow=False, reason="mode-command", command=command)
            return GateDecision(allow=True, reason="always-mode", stripped_text=stripped)

        mentioned = is_self_mentioned(message)
        if mentioned:
            stripped = strip_self_mentions(raw_text, message)
            command = parse_mode_command(stripped)
            if command is not None:
                # A mode command is an instruction to us, not a prompt for the
                # agent — handle it and stop.
                return GateDecision(allow=False, reason="mode-command", command=command)
            self.window.record(chat_id, user_id, now)
            return GateDecision(allow=True, reason="mentioned", stripped_text=stripped)

        if self.window.is_open(chat_id, user_id, now):
            self.window.record(chat_id, user_id, now)
            return GateDecision(
                allow=True,
                reason="followup-window",
                stripped_text=raw_text if is_text else None,
            )

        return GateDecision(allow=False, reason="no-mention")

    async def handle_command(
        self,
        client: Any,
        command: str,
        chat_id: str,
        user_id: str,
        reply_token: str = "",
    ) -> None:
        """Apply a mode command and reply with the current state + buttons."""
        if command in VALID_MODES:
            self.store.set_mode(chat_id, command, user_id)
            logger.info(
                "LINE: response mode for %s set to %r by %s", chat_id, command, user_id
            )
        await self._send_mode_card(client, chat_id, reply_token)

    async def handle_postback(
        self,
        client: Any,
        data: Any,
        chat_id: str,
        user_id: str,
        reply_token: str = "",
    ) -> bool:
        """Handle a tapped mode button. ``False`` means "not ours, carry on"."""
        mode = parse_mode_postback(data)
        if mode is None:
            return False
        self.store.set_mode(chat_id, mode, user_id)
        logger.info(
            "LINE: response mode for %s set to %r by %s (button)", chat_id, mode, user_id
        )
        await self._send_mode_card(client, chat_id, reply_token)
        return True

    async def _send_mode_card(
        self, client: Any, chat_id: str, reply_token: str = ""
    ) -> None:
        """Send the mode card as a raw LINE message.

        Template payloads go through ``_LineClient.reply``/``push``, not
        ``LineAdapter.send`` (which is text-oriented) — same path upstream's
        own ``build_postback_button_message`` takes. Prefers ``reply``: reply
        tokens are free, pushes count against the channel's monthly quota.
        """
        payload = build_mode_buttons_message(chat_id, self.store.get_mode(chat_id))
        try:
            if reply_token:
                await client.reply(reply_token, [payload])
            else:
                await client.push(chat_id, [payload])
        except Exception as exc:
            # The mode change already landed on disk; failing to confirm it is
            # not a reason to raise into the adapter's event dispatch.
            logger.warning("LINE: mode confirmation send failed for %s: %s", chat_id, exc)
