"""python -m harness.server [--port 8765] [--allow-unblocked] [--fake-default] [--policy-default P] [--confirm-timeout S]

Serves the web UI on 127.0.0.1 only. The token comes from HARNESS_UI_TOKEN
or is generated here and printed once, inside the URL to open.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys

import uvicorn

from harness.contracts import Config, ConfigError
from harness.server.app import create_app

HOST = "127.0.0.1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m harness.server", description="harness web UI (localhost only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--allow-unblocked", action="store_true",
                    help="allow freeform goals when host loopback is not blocked")
    ap.add_argument("--fake-default", action="store_true", help="tick the Fake toggle by default in the page")
    ap.add_argument("--policy-default", choices=("ui", "approve", "reject"), default="ui",
                    help="confirmation policy preselected in the page (default ui)")
    ap.add_argument("--confirm-timeout", type=float, default=300.0,
                    help="seconds a `ui` confirmation waits before it resolves to reject")
    args = ap.parse_args(argv)
    if args.confirm_timeout <= 0:
        print("--confirm-timeout must be positive", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        config = Config.from_env()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    token = os.environ.get("HARNESS_UI_TOKEN") or secrets.token_urlsafe(24)
    app = create_app(config, token, allow_unblocked=args.allow_unblocked,
                     confirm_timeout_s=args.confirm_timeout, fake_default=args.fake_default,
                     policy_default=args.policy_default)
    print(f"harness UI: http://{HOST}:{args.port}/?token={token}", flush=True)
    # access_log off: request lines would otherwise log the token in the query string
    uvicorn.run(app, host=HOST, port=args.port, access_log=False, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
