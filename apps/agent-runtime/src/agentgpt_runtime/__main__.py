"""Entry point: NDJSON loop over stdin/stdout.

Usage: uv run python -m agentgpt_runtime

Protocol messages go to stdout (one JSON object per line). All logging goes
to stderr. The process exits 0 on runtime.shutdown or clean stdin EOF.
"""

from __future__ import annotations

import logging
import sys

from agentgpt_runtime.chat import ChatRuns
from agentgpt_runtime.protocol import ProtocolError, encode, make_error, parse_line
from agentgpt_runtime.server import configure_chat_runs, dispatch, should_exit

logger = logging.getLogger(__name__)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logger.info("agent-runtime sidecar starting")
    configure_chat_runs(ChatRuns(encode))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            envelope = parse_line(line)
        except ProtocolError as exc:
            logger.warning("rejected input: %s", exc.message)
            encode(make_error(exc.request_id or "unknown", exc.code, exc.message))
            continue

        logger.debug("received %s (request_id=%s)", envelope.type, envelope.request_id)
        response = dispatch(envelope)
        if response is not None:
            encode(response)
        if should_exit(envelope):
            logger.info("shutting down cleanly")
            return 0

    logger.info("stdin closed; exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
