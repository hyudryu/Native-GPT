"""Shared fixtures and helpers for sidecar tests."""

from __future__ import annotations

import json
import queue
import socket
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parents[1]
SCHEMA_DIR = REPO_ROOT / "packages" / "protocol-types" / "schemas"

READ_TIMEOUT_SECONDS = 15


@pytest.fixture(scope="session")
def envelope_schema() -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / "envelope.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def messages_schema() -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / "messages.json").read_text(encoding="utf-8"))


def validate_envelope(message: dict[str, Any], envelope_schema: dict[str, Any]) -> None:
    """Validate a message against envelope.json.

    NOTE(deviation): the authored schema's `type` pattern "^[a-z]+\\.[a-z_]+$"
    only allows a single dot, so it rejects response types such as
    "runtime.hello.ok". We relax the pattern to allow multi-segment types for
    validation only; the schema file itself is left untouched.
    """
    import copy

    import jsonschema

    schema = copy.deepcopy(envelope_schema)
    schema["properties"]["type"]["pattern"] = r"^[a-z]+(\.[a-z_]+)+$"
    jsonschema.validate(message, schema)


def make_request(msg_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "protocol": "1.0",
        "type": msg_type,
        "request_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": payload or {},
    }


class Sidecar:
    """Spawned agent-runtime sidecar with line-oriented stdio."""

    def __init__(self, capture_stderr: bool = False) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "agentgpt_runtime"],
            cwd=APP_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
            # The sidecar's protocol channel is UTF-8 (it reconfigures stdout
            # itself); decode as such regardless of the host locale (cp1252
            # on Windows) or non-ASCII model output turns into mojibake.
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._lines: queue.Queue[str] = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self._stderr_chunks: list[str] = []
        if capture_stderr:
            threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr_chunks.append(line)

    def stderr_text(self) -> str:
        return "".join(self._stderr_chunks)

    def send(self, message: dict[str, Any] | str) -> None:
        assert self.proc.stdin is not None
        line = message if isinstance(message, str) else json.dumps(message)
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read_message(self, timeout: float = READ_TIMEOUT_SECONDS) -> dict[str, Any]:
        line = self._lines.get(timeout=timeout)
        return json.loads(line)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=READ_TIMEOUT_SECONDS)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None


@pytest.fixture()
def sidecar() -> Any:
    sc = Sidecar()
    try:
        yield sc
    finally:
        sc.close()


# --- Mock OpenAI-style model server (stdlib http.server, no extra deps) ---


class _MockOpenAIHandler(BaseHTTPRequestHandler):
    """Behavior is driven by attributes on the server instance (see fixture)."""

    def do_GET(self) -> None:
        srv = self.server
        srv.last_path = self.path
        srv.last_auth = self.headers.get("Authorization")
        if srv.require_auth and srv.last_auth != f"Bearer {srv.expected_key}":
            self._respond(401, b'{"error": {"message": "unauthorized"}}')
        else:
            self._respond(srv.status, srv.response_body)

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence request logging
        pass


@pytest.fixture()
def mock_server() -> Any:
    """ThreadingHTTPServer mock of an OpenAI-style /v1/models endpoint."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockOpenAIHandler)
    server.status = 200
    server.response_body = json.dumps(
        {
            "object": "list",
            "data": [
                {"id": "gpt-4o", "object": "model", "owned_by": "acme"},
                {"id": "llama-3.1-8b", "object": "model", "extra_meta": {"ctx": 8192}},
            ],
        }
    ).encode()
    server.require_auth = False
    server.expected_key = None
    server.last_path = None
    server.last_auth = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=READ_TIMEOUT_SECONDS)


def mock_base_url(mock_server: Any) -> str:
    return f"http://127.0.0.1:{mock_server.server_address[1]}"


def unused_port() -> int:
    """A port that is (almost certainly) not listening, for refusal tests."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
