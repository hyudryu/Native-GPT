"""Subprocess round-trip tests: drive the sidecar over NDJSON stdio."""

from __future__ import annotations

import json

import jsonschema

from conftest import Sidecar, make_request, mock_base_url, validate_envelope


def validate_payload(messages_schema: dict, payload: dict, def_name: str) -> None:
    jsonschema.validate(payload, messages_schema["$defs"][def_name])


def test_hello_roundtrip(sidecar: Sidecar, envelope_schema: dict, messages_schema: dict) -> None:
    request = make_request("runtime.hello", {"client": "pytest", "client_version": "0.0.1"})
    sidecar.send(request)

    response = sidecar.read_message()
    validate_envelope(response, envelope_schema)
    assert response["type"] == "runtime.hello.ok"
    assert response["request_id"] == request["request_id"]

    validate_payload(messages_schema, response["payload"], "runtime.hello.ok")
    assert response["payload"]["runtime"] == "agentgpt-runtime"
    assert response["payload"]["protocol"] == "1.0"
    assert "chat" in response["payload"]["capabilities"]


def test_health_reports_rss(sidecar: Sidecar, envelope_schema: dict, messages_schema: dict) -> None:
    request = make_request("runtime.health")
    sidecar.send(request)

    response = sidecar.read_message()
    validate_envelope(response, envelope_schema)
    assert response["type"] == "runtime.health.ok"
    assert response["request_id"] == request["request_id"]

    validate_payload(messages_schema, response["payload"], "runtime.health.ok")
    assert response["payload"]["status"] == "ok"
    assert response["payload"]["rss_bytes"] > 0
    assert response["payload"]["uptime_seconds"] >= 0


def test_unknown_type_is_ignored(sidecar: Sidecar) -> None:
    sidecar.send(make_request("run.text_delta", {"text": "not handled in phase 0"}))
    assert sidecar.alive

    # The loop is synchronous and ordered: the next line we read must be the
    # response to this hello, proving the unknown type produced no output.
    hello = make_request("runtime.hello", {"client": "pytest", "client_version": "0.0.1"})
    sidecar.send(hello)
    response = sidecar.read_message()
    assert response["type"] == "runtime.hello.ok"
    assert response["request_id"] == hello["request_id"]
    assert sidecar.alive


def test_malformed_json_gets_error_response(sidecar: Sidecar, messages_schema: dict) -> None:
    sidecar.send("this is not json {")

    response = sidecar.read_message()
    assert response["type"] == "error"
    assert response["payload"]["code"] == "bad_request"
    assert response["payload"]["retryable"] is False
    validate_payload(messages_schema, response["payload"], "error")
    assert sidecar.alive


def test_wrong_protocol_version_gets_error(sidecar: Sidecar) -> None:
    request = make_request("runtime.health")
    request["protocol"] = "2.0"
    sidecar.send(request)

    response = sidecar.read_message()
    assert response["type"] == "error"
    assert response["request_id"] == request["request_id"]
    assert response["payload"]["code"] == "unsupported_protocol"
    assert response["payload"]["retryable"] is False
    assert sidecar.alive


def test_shutdown_exits_zero(sidecar: Sidecar) -> None:
    request = make_request("runtime.shutdown")
    sidecar.send(request)

    ack = sidecar.read_message()
    assert ack["type"] == "runtime.shutdown"
    assert ack["request_id"] == request["request_id"]
    assert ack["payload"] == {}

    assert sidecar.proc.wait(timeout=15) == 0


def test_endpoint_test_roundtrip(
    sidecar: Sidecar, mock_server, envelope_schema: dict, messages_schema: dict
) -> None:
    request = make_request("endpoint.test", {"base_url": mock_base_url(mock_server)})
    sidecar.send(request)

    response = sidecar.read_message()
    validate_envelope(response, envelope_schema)
    assert response["type"] == "endpoint.test.ok"
    assert response["request_id"] == request["request_id"]

    validate_payload(messages_schema, response["payload"], "endpoint.test.ok")
    assert response["payload"]["ok"] is True
    assert response["payload"]["latency_ms"] >= 0


def test_models_list_roundtrip(
    sidecar: Sidecar, mock_server, envelope_schema: dict, messages_schema: dict
) -> None:
    request = make_request("models.list", {"base_url": mock_base_url(mock_server)})
    sidecar.send(request)

    response = sidecar.read_message()
    validate_envelope(response, envelope_schema)
    assert response["type"] == "models.list.ok"
    assert response["request_id"] == request["request_id"]

    validate_payload(messages_schema, response["payload"], "models.list.ok")
    ids = [m["id"] for m in response["payload"]["models"]]
    assert ids == ["gpt-4o", "llama-3.1-8b"]
    assert response["payload"]["models"][0]["raw"]["owned_by"] == "acme"
    assert response["payload"]["fetched_at"]


def test_models_list_invalid_response_over_wire(
    sidecar: Sidecar, mock_server, messages_schema: dict
) -> None:
    mock_server.response_body = b"definitely not json"
    request = make_request("models.list", {"base_url": mock_base_url(mock_server)})
    sidecar.send(request)

    response = sidecar.read_message()
    assert response["type"] == "error"
    assert response["request_id"] == request["request_id"]
    assert response["payload"]["code"] == "invalid_response"
    validate_payload(messages_schema, response["payload"], "error")


def test_api_key_never_appears_in_envelopes_or_logs(mock_server) -> None:
    secret = "sk-supersecret-do-not-leak"
    mock_server.require_auth = True
    mock_server.expected_key = secret

    sc = Sidecar(capture_stderr=True)
    try:
        # Successful authed call.
        sc.send(
            make_request(
                "endpoint.test", {"base_url": mock_base_url(mock_server), "api_key": secret}
            )
        )
        ok_response = json.dumps(sc.read_message())
        # Failing authed call (bad key path also exercises error envelopes).
        mock_server.response_body = b"oops not json"
        sc.send(
            make_request("models.list", {"base_url": mock_base_url(mock_server), "api_key": secret})
        )
        err_response = json.dumps(sc.read_message())

        assert mock_server.last_auth == f"Bearer {secret}"
        assert secret not in ok_response
        assert secret not in err_response

        sc.send(make_request("runtime.shutdown"))
        sc.read_message()  # shutdown ack
        assert sc.proc.wait(timeout=15) == 0
        assert secret not in sc.stderr_text()
    finally:
        sc.close()
