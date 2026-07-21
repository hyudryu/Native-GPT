"""End-to-end streaming chat tests against a mock OpenAI SSE server.

Regression coverage for the stdout-pollution bug: Strands' default callback
handler printed streamed text to stdout, corrupting the NDJSON protocol
channel — and crashed outright on non-ASCII output under Windows cp1252.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from conftest import READ_TIMEOUT_SECONDS, Sidecar, make_request

CHUNKS = ["Hello", " ✅", " wörld", "!"]


class _SSEHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        assert self.path == "/v1/chat/completions", self.path
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = b""
        for chunk in CHUNKS:
            delta = json.dumps(
                {"choices": [{"delta": {"content": chunk}}]}
            ).encode()
            body += b"data: " + delta + b"\n\n"
        body += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture()
def sse_server() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=READ_TIMEOUT_SECONDS)


def _run_start_payload(base_url: str) -> dict[str, Any]:
    return {
        "run_id": "run-stream-test",
        "conversation_id": "conv-1",
        "message_id": "msg-1",
        "prompt": "hi",
        "history": [],
        "model": {"base_url": base_url, "model_id": "mock-model"},
    }


def test_streaming_run_deltas_and_protocol_purity(sse_server: Any) -> None:
    """Every stdout line must be valid JSON (no leaked print output), and the
    emoji/non-ASCII deltas must arrive intact."""
    sidecar = Sidecar(capture_stderr=True)
    try:
        base_url = f"http://127.0.0.1:{sse_server.server_address[1]}"
        request = make_request("run.start", _run_start_payload(base_url))
        sidecar.send(request)

        messages: list[dict[str, Any]] = []
        # read_message() json.loads each line: any polluted line fails here.
        messages.append(sidecar.read_message())  # run.started ack
        assert messages[0]["type"] == "run.started"

        while True:
            msg = sidecar.read_message()
            messages.append(msg)
            if msg["type"] in {"run.completed", "run.failed"}:
                break

        types = [m["type"] for m in messages]
        assert "run.failed" not in types, f"run failed: {messages[-1]}"
        assert types[-1] == "run.completed"

        deltas = [m["payload"]["text"] for m in messages if m["type"] == "run.text_delta"]
        assert "".join(deltas) == "".join(CHUNKS)
        # All stream messages correlate with the original request.
        assert all(m["request_id"] == request["request_id"] for m in messages)
    finally:
        sidecar.close()
