"""Entry point: run the bridge as a uvicorn server.

Usage::

    python -m agentgpt_bridge [--host HOST] [--port PORT] [--fake]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import BridgeConfig, resolve_token


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentGPT remote backend host (bridge)")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8443, help="bind port (default: 8443)")
    parser.add_argument("--fake", action="store_true", help="use fake workloads (no GPU)")
    parser.add_argument(
        "--log-level", default="info", help="log level (default: info)"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    token = resolve_token()
    # If the token was generated (not from env), print it once to stderr so the
    # operator can copy it into the desktop's remote host config.
    if not os.environ.get("AGENTGPT_BRIDGE_TOKEN", "").strip():
        print(f"Generated bridge token: {token}", file=sys.stderr)
        print("Set AGENTGPT_BRIDGE_TOKEN to use a fixed token.", file=sys.stderr)

    config = BridgeConfig(
        host=args.host,
        port=args.port,
        token=token,
        use_fake_workloads=args.fake,
    )

    # Set the token env var so the auth dependency can read it.
    os.environ["AGENTGPT_BRIDGE_TOKEN"] = token

    import uvicorn

    uvicorn.run(
        _create_app_factory(config),
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
    )


def _create_app_factory(config: BridgeConfig):
    """Return a factory that builds the app with the given config."""
    def factory():
        from .app import create_app
        return create_app(config)
    return factory


if __name__ == "__main__":
    main()
