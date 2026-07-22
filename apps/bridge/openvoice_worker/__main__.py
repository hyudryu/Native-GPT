"""Entry point for the OpenVoice worker subprocess.

Run via the bridge's workload manager::

    python -m openvoice_worker --port 8200 --checkpoints /opt/OpenVoice/checkpoints_v2 \
        --voices-dir /var/lib/agentgpt/voices
"""

from __future__ import annotations

import argparse
import logging
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenVoice worker for AgentGPT bridge")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--checkpoints", required=False, default="/opt/OpenVoice/checkpoints_v2")
    parser.add_argument("--voices-dir", required=False, default="/var/lib/agentgpt/voices")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    from . import _init_paths

    _init_paths(args.checkpoints, args.voices_dir)

    import importlib

    import uvicorn

    mod = importlib.import_module("openvoice_worker")
    uvicorn.run(mod.app, host=args.host, port=args.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
