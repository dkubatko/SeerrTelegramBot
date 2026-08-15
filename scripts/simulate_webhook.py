"""POST a realistic Seerr webhook at the bot, without running Seerr.

    python scripts/simulate_webhook.py pending
    python scripts/simulate_webhook.py test --url http://127.0.0.1:8420/webhook
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from mock_seerr import REQUESTS, TEST_PAYLOAD, pending_payload  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["pending", "test"], nargs="?", default="pending")
    parser.add_argument("--url", default="http://127.0.0.1:8420/webhook")
    parser.add_argument("--id", default="1", help="mock request id for 'pending'")
    parser.add_argument("--auth", default=None, help="Authorization header value")
    args = parser.parse_args()

    if args.kind == "test":
        payload = TEST_PAYLOAD
    else:
        record = REQUESTS.get(args.id)
        if record is None:
            print(f"No mock request with id {args.id}; try 1 or 2.", file=sys.stderr)
            return 2
        payload = pending_payload(record)

    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        args.url, data=body, headers={"Content-Type": "application/json"}
    )
    if args.auth:
        request.add_header("Authorization", args.auth)

    print(f"POST {args.url}\n{json.dumps(payload, indent=2)}\n")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            print(f"<- HTTP {response.status} {response.read().decode().strip()}")
            return 0
    except urllib.error.HTTPError as exc:
        print(f"<- HTTP {exc.code} {exc.read().decode().strip()}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Could not reach the bot: {exc.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
