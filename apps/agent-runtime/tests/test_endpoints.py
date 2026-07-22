"""Tests for endpoints.py: URL resolution, endpoint.test, models.list."""

from __future__ import annotations

import pytest

from agentgpt_runtime.endpoints import (
    EndpointError,
    list_models,
    resolve_models_url,
)
from agentgpt_runtime.endpoints import test_endpoint as probe_endpoint
from agentgpt_runtime.protocol import EndpointTestPayload, ModelsListPayload
from conftest import mock_base_url, unused_port

# --- URL resolution ---


@pytest.mark.parametrize(
    ("base_url", "path", "expected"),
    [
        ("http://localhost:1234", None, "http://localhost:1234/v1/models"),
        ("http://localhost:1234/", None, "http://localhost:1234/v1/models"),
        ("http://localhost:1234/v1", None, "http://localhost:1234/v1/models"),
        ("http://localhost:1234/v1/", None, "http://localhost:1234/v1/models"),
        ("http://localhost:1234/v1//", None, "http://localhost:1234/v1/models"),
        ("https://api.example.com", None, "https://api.example.com/v1/models"),
        ("https://api.example.com:8443/v1", None, "https://api.example.com:8443/v1/models"),
        ("http://host", "/api/models", "http://host/api/models"),
        ("http://host/", "api/models", "http://host/api/models"),
        ("http://host:9999", "/v2/models/", "http://host:9999/v2/models"),
    ],
)
def test_resolve_models_url(base_url: str, path: str | None, expected: str) -> None:
    if path is None:
        assert resolve_models_url(base_url) == expected
    else:
        assert resolve_models_url(base_url, path) == expected


def test_resolve_models_url_never_duplicates_v1() -> None:
    for base in ("http://h/v1", "http://h/v1/", "http://h:8080/v1//"):
        url = resolve_models_url(base)
        assert "/v1/v1" not in url
        assert url.endswith("/v1/models")


# --- endpoint.test ---


def _test_payload(base_url: str, **kw: object) -> EndpointTestPayload:
    return EndpointTestPayload.model_validate({"base_url": base_url, **kw})


def test_endpoint_test_success(mock_server) -> None:
    result = probe_endpoint(_test_payload(mock_base_url(mock_server)))
    assert result.ok is True
    assert result.latency_ms is not None and result.latency_ms >= 0
    # BaseHTTPRequestHandler sends a Server header by default.
    assert result.server
    assert result.error is None
    assert mock_server.last_path == "/v1/models"


def test_endpoint_test_auth_error(mock_server) -> None:
    mock_server.status = 401
    result = probe_endpoint(_test_payload(mock_base_url(mock_server)))
    assert result.ok is False
    assert result.latency_ms is not None
    assert result.error is not None
    assert result.error.code == "auth_error"
    assert result.error.retryable is False


def test_endpoint_test_http_error(mock_server) -> None:
    mock_server.status = 500
    result = probe_endpoint(_test_payload(mock_base_url(mock_server)))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "http_error"
    assert result.error.retryable is True


def test_endpoint_test_connection_error() -> None:
    result = probe_endpoint(_test_payload(f"http://127.0.0.1:{unused_port()}", timeout_seconds=5))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "connection_error"
    assert result.error.retryable is True


def test_endpoint_test_invalid_response(mock_server) -> None:
    mock_server.response_body = b"<html>not json</html>"
    result = probe_endpoint(_test_payload(mock_base_url(mock_server)))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_response"


def test_endpoint_test_sends_bearer_and_never_echoes_key(mock_server) -> None:
    secret = "sk-test-secret-key"
    result = probe_endpoint(_test_payload(mock_base_url(mock_server), api_key=secret))
    assert result.ok is True
    assert mock_server.last_auth == f"Bearer {secret}"
    assert secret not in result.model_dump_json()


# --- models.list ---


def _list_payload(base_url: str, **kw: object) -> ModelsListPayload:
    return ModelsListPayload.model_validate({"base_url": base_url, **kw})


def test_list_models_parses_ids_and_preserves_raw(mock_server) -> None:
    result = list_models(_list_payload(mock_base_url(mock_server)))
    assert [m.id for m in result.models] == ["gpt-4o", "llama-3.1-8b"]
    assert result.models[0].raw["owned_by"] == "acme"
    # Nonstandard extra metadata is tolerated and preserved in raw.
    assert result.models[1].raw["extra_meta"] == {"ctx": 8192}
    assert result.fetched_at


def test_list_models_base_url_with_v1_suffix(mock_server) -> None:
    result = list_models(_list_payload(mock_base_url(mock_server) + "/v1/"))
    assert mock_server.last_path == "/v1/models"
    assert len(result.models) == 2


def test_list_models_custom_path(mock_server) -> None:
    result = list_models(_list_payload(mock_base_url(mock_server), model_list_path="/models"))
    assert mock_server.last_path == "/models"
    assert len(result.models) == 2


def test_list_models_bad_json_raises_invalid_response(mock_server) -> None:
    mock_server.response_body = b"not json at all"
    with pytest.raises(EndpointError, match="valid JSON") as exc_info:
        list_models(_list_payload(mock_base_url(mock_server)))
    assert exc_info.value.code == "invalid_response"


def test_list_models_missing_data_list_raises_invalid_response(mock_server) -> None:
    mock_server.response_body = b'{"models": [{"id": "x"}]}'
    with pytest.raises(EndpointError) as exc_info:
        list_models(_list_payload(mock_base_url(mock_server)))
    assert exc_info.value.code == "invalid_response"


def test_list_models_sends_bearer_header(mock_server) -> None:
    secret = "sk-list-secret"
    mock_server.require_auth = True
    mock_server.expected_key = secret
    result = list_models(_list_payload(mock_base_url(mock_server), api_key=secret))
    assert mock_server.last_auth == f"Bearer {secret}"
    assert len(result.models) == 2


def test_list_models_auth_failure_raises(mock_server) -> None:
    mock_server.require_auth = True
    mock_server.expected_key = "sk-correct"
    with pytest.raises(EndpointError) as exc_info:
        list_models(_list_payload(mock_base_url(mock_server), api_key="sk-wrong"))
    assert exc_info.value.code == "auth_error"
    # The wrong key must not leak into the error message.
    assert "sk-wrong" not in exc_info.value.message


# --- tls_verify ---


def _spy_httpx_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace httpx.Client with a recording fake; returns captured kwargs."""
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def json(self) -> dict:
            return {"data": [{"id": "m-1"}]}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str, headers: dict | None = None) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("agentgpt_runtime.endpoints.httpx.Client", FakeClient)
    return captured


def test_tls_verify_false_disables_httpx_verification(monkeypatch) -> None:
    captured = _spy_httpx_client(monkeypatch)
    result = list_models(_list_payload("https://selfsigned.local", tls_verify=False))
    assert [m.id for m in result.models] == ["m-1"]
    assert captured["verify"] is False

    result = probe_endpoint(_test_payload("https://selfsigned.local", tls_verify=False))
    assert result.ok is True
    assert captured["verify"] is False


def test_tls_verify_absent_defaults_to_secure(monkeypatch) -> None:
    captured = _spy_httpx_client(monkeypatch)
    list_models(_list_payload("https://api.example.com"))
    assert captured["verify"] is True

    result = probe_endpoint(_test_payload("https://api.example.com"))
    assert result.ok is True
    assert captured["verify"] is True
