#!/opt/hermes/.venv/bin/python
"""Mint a one-off LINE join invite QR code for a manager to hand to a new hire.

Run with the Hermes venv's python3 so `qrcode` and the patched
`LineInviteStore` are importable:

    /opt/hermes/.venv/bin/python3 generate_invite.py \\
        --label "Jamie - PT cashier" --created-by U1234567890abcdef \\
        [--hours 48] [--out-dir /tmp]

Prints one JSON object to stdout: {"token", "url", "qr_path", "label",
"expires_at"} on success, or {"error": "..."} on failure (exit code 1).

The token is only ever available in plaintext here — LineInviteStore
persists just a salted hash — so this script is the only place it can be
captured. It never sends anything over the network itself; the caller
(the agent, via the SKILL.md instructions) is responsible for delivering
the QR PNG to the manager currently in the chat.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

# This script runs standalone (not via the `hermes` CLI), so /opt/hermes
# isn't automatically on sys.path — add it so `plugins.platforms.line.adapter`
# and `hermes_constants` resolve the same way they do inside the gateway
# process itself.
_HERMES_ROOT = os.environ.get("HERMES_ROOT", "/opt/hermes")
if _HERMES_ROOT not in sys.path:
    sys.path.insert(0, _HERMES_ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label", required=True,
        help="The invitee's actual name (role optional), e.g. 'Jamie - PT cashier'. "
             "Required: the LINE adapter uses this as the approved-user's display "
             "name whenever LINE's own profile lookup doesn't resolve in time, so "
             "a role-only or placeholder value here leaks into 'Approved users'.",
    )
    parser.add_argument(
        "--created-by", default="",
        help="The requesting manager's LINE user ID (U...), if known. "
             "Used to send a '<label> just joined!' notification on redemption; "
             "omit only if the manager's LINE user ID genuinely isn't available.",
    )
    parser.add_argument("--hours", type=float, default=48.0, help="Invite TTL in hours (default: 48)")
    parser.add_argument("--out-dir", default="/tmp", help="Directory to write the QR PNG to (default: /tmp)")
    args = parser.parse_args()

    basic_id = os.environ.get("LINE_BASIC_ID", "").strip()
    if not basic_id:
        print(json.dumps({
            "error": "LINE_BASIC_ID is not set. Add it in the Render Environment tab "
                     "(LINE Developers Console > Messaging API > Basic Settings > Basic ID, e.g. @abc1234)."
        }))
        return 1

    try:
        import qrcode
    except ImportError:
        print(json.dumps({"error": "qrcode is not installed in this environment"}))
        return 1

    from plugins.platforms.line.adapter import LineInviteStore

    now = time.time()
    store = LineInviteStore()
    token = store.mint(args.label, args.created_by, ttl_hours=args.hours)

    url = f"https://line.me/R/oaMessage/{quote(basic_id, safe='')}/?{quote(token, safe='')}"

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    qr_path = out_dir / f"line-invite-{int(now)}.png"
    qrcode.make(url).save(qr_path)

    print(json.dumps({
        "token": token,
        "url": url,
        "qr_path": str(qr_path),
        "label": args.label,
        "expires_at": now + args.hours * 3600,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
