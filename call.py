"""
call.py — place an outbound Vobiz call into the Deepgram agent.

    python call.py                          # dial TO_NUMBER from .env
    python call.py --to +919XXXXXXXXX
    python call.py --host abc123.ngrok-free.app   # override PUBLIC_HOSTNAME
    python call.py --dry-run                # print the payload without dialling

Inbound calls need none of this — create a Voice Application pointing at
https://PUBLIC_HOSTNAME/answer and attach a number to it. This is for dialling out.

Start `python app.py` and the tunnel first; Vobiz fetches the answer URL the
moment the callee picks up.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

API_BASE = "https://api.vobiz.ai/api/v1"
AUTH_ID = os.getenv("VOBIZ_AUTH_ID", "")
AUTH_TOKEN = os.getenv("VOBIZ_AUTH_TOKEN", "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", default=os.getenv("TO_NUMBER", ""), help="destination number")
    parser.add_argument("--from", dest="from_", default=os.getenv("FROM_NUMBER", ""),
                        help="a DID this account owns")
    parser.add_argument("--host", default=os.getenv("PUBLIC_HOSTNAME", ""),
                        help="public host running app.py, no scheme")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the payload and exit without placing a call")
    args = parser.parse_args()

    if not AUTH_ID or not AUTH_TOKEN:
        sys.exit("Set VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN in .env")
    # The template ships these as +91XXXXXXXXXX, which is truthy -- so a bare
    # `python call.py` straight after `cp .env.example .env` would otherwise post a
    # placeholder to the live Call API and bill the attempt.
    for label, value in (("TO_NUMBER/--to", args.to), ("FROM_NUMBER/--from", args.from_)):
        if not value:
            sys.exit(f"Set {label}")
        if "X" in value.upper():
            sys.exit(f"{label} is still the placeholder {value!r} -- set a real number")
    if not args.host:
        sys.exit("Set PUBLIC_HOSTNAME in .env or pass --host")

    # Normalise exactly as app.py does, so the same .env works for both. Without
    # this a PUBLIC_HOSTNAME of "https://host" builds "https://https://host/answer",
    # which the API accepts and the call then dies on.
    host = args.host.removeprefix("https://").removeprefix("http://").rstrip("/")
    if "/" in host:
        sys.exit(f"PUBLIC_HOSTNAME must be a bare hostname, got {args.host!r}")

    base = f"https://{host}"
    payload = {
        "from": args.from_,
        "to": args.to,
        "answer_url": f"{base}/answer",
        "answer_method": "POST",
        "hangup_url": f"{base}/hangup",
        "hangup_method": "POST",
    }
    print(json.dumps(payload, indent=2))
    if args.dry_run:
        print("\n--dry-run: nothing was dialled.")
        return

    response = requests.post(
        f"{API_BASE}/Account/{AUTH_ID}/Call/",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "X-Auth-ID": AUTH_ID,
            "X-Auth-Token": AUTH_TOKEN,
        },
        timeout=30,
    )
    print(f"\nHTTP {response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2))
    except ValueError:
        print(response.text[:500])

    if response.status_code >= 400:
        # Read the failure before reaching for the code:
        #   401                          credentials are wrong or the account is dead
        #   402                          insufficient balance
        #   "from number ... not owned"  the DID belongs to a different account
        sys.exit(1)

    print("\nAnswer the phone and talk. Watch app.py for [user] / [assistant] lines.")


if __name__ == "__main__":
    main()
