"""In-process tests: dispatch behavior, memory smoke, SDK availability."""

from __future__ import annotations

import importlib.util
import tracemalloc

from agentgpt_runtime.protocol import (
    TYPE_HEALTH,
    TYPE_HEALTH_OK,
    TYPE_HELLO,
    TYPE_HELLO_OK,
    Envelope,
    ProtocolError,
    make_envelope,
    parse_line,
)
from agentgpt_runtime.server import dispatch


def test_dispatch_hello_and_health() -> None:
    hello = make_envelope(TYPE_HELLO, "req-1", {"client": "test", "client_version": "1.0"})
    response = dispatch(hello)
    assert response is not None
    assert response.type == TYPE_HELLO_OK
    assert response.request_id == "req-1"

    health = make_envelope(TYPE_HEALTH, "req-2", {})
    response = dispatch(health)
    assert response is not None
    assert response.type == TYPE_HEALTH_OK
    assert response.payload["rss_bytes"] > 0


def test_dispatch_unknown_returns_none() -> None:
    envelope = make_envelope("event.unknown", "req-3", {"run_id": "x"})
    assert dispatch(envelope) is None


def test_parse_line_rejects_malformed_json() -> None:
    try:
        parse_line("{nope")
    except ProtocolError as exc:
        assert exc.code == "bad_request"
    else:  # pragma: no cover
        raise AssertionError("expected ProtocolError")


def test_parse_line_rejects_wrong_protocol() -> None:
    envelope = make_envelope(TYPE_HEALTH, "req-4", {})
    line = envelope.model_dump_json().replace('"1.0"', '"2.0"', 1)
    try:
        parse_line(line)
    except ProtocolError as exc:
        assert exc.code == "unsupported_protocol"
        assert exc.request_id == "req-4"
    else:  # pragma: no cover
        raise AssertionError("expected ProtocolError")


def test_memory_smoke_hello_health_cycles() -> None:
    """Handle 100 hello/health cycles; assert no monotonic memory growth.

    Loose harness skeleton: compares peak allocations of the second half of
    the cycles against the first half with a generous allowance.
    """
    hello = make_envelope(TYPE_HELLO, "req-m", {"client": "test", "client_version": "1.0"})
    health = make_envelope(TYPE_HEALTH, "req-m", {})

    def cycle() -> None:
        dispatch(hello)
        dispatch(health)

    cycle()  # warm up caches/imports
    tracemalloc.start()
    for _ in range(50):
        cycle()
    first_current, _ = tracemalloc.get_traced_memory()
    for _ in range(50):
        cycle()
    second_current, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    growth = second_current - first_current
    assert growth < 256 * 1024, f"memory grew by {growth} bytes over 50 cycles"


def test_strands_sdk_available_but_not_imported_at_startup() -> None:
    import sys

    assert "strands" not in sys.modules, "strands must not be imported at startup"
    assert importlib.util.find_spec("strands") is not None


def test_envelope_model_roundtrip() -> None:
    envelope = Envelope.model_validate(make_envelope("runtime.health", "req-5", {}).model_dump())
    assert envelope.protocol == "1.0"
    assert envelope.sequence is None
