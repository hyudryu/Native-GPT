"""Endpoint connectivity testing and model listing (OpenAI-style servers).

Security note: payloads may carry a raw api_key resolved by the host secret
broker. The key is only ever placed in the Authorization header of the
outbound request; it must never appear in responses, errors, or logs.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from agentgpt_runtime.protocol import (
    EndpointTestOkPayload,
    EndpointTestPayload,
    ErrorPayload,
    ModelEntry,
    ModelsListOkPayload,
    ModelsListPayload,
    utc_now_iso,
)

DEFAULT_MODEL_LIST_PATH = "/v1/models"


class EndpointError(Exception):
    """Wire-level failure talking to a model server."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def resolve_models_url(base_url: str, model_list_path: str = DEFAULT_MODEL_LIST_PATH) -> str:
    """Resolve the models endpoint URL for an OpenAI-style server.

    - Trailing slashes on base_url are stripped.
    - If base_url's path already ends with "/v1", append just "/models".
    - Otherwise append model_list_path (default "/v1/models").
    Never produces duplicated segments like "/v1/v1/models".
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return base + "/models"
    path = "/" + model_list_path.strip("/")
    return base + path


def _fetch_json(url: str, api_key: str | None, timeout_seconds: int) -> tuple[Any, httpx.Response]:
    """GET url and parse the body as JSON.

    Raises EndpointError with a wire-level code on any failure. Error
    messages deliberately contain only the URL and status/exception class —
    never the api_key.
    """
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise EndpointError(
            "timeout",
            f"request to {url} timed out after {timeout_seconds}s",
            retryable=True,
        ) from exc
    except httpx.TransportError as exc:
        raise EndpointError(
            "connection_error",
            f"could not connect to {url} ({exc.__class__.__name__})",
            retryable=True,
        ) from exc

    status = response.status_code
    if status in (401, 403):
        raise EndpointError("auth_error", f"server returned HTTP {status} for {url}")
    if status >= 400:
        raise EndpointError(
            "http_error",
            f"server returned HTTP {status} for {url}",
            retryable=status >= 500,
        )
    try:
        return response.json(), response
    except json.JSONDecodeError as exc:
        raise EndpointError(
            "invalid_response", f"server at {url} did not return valid JSON"
        ) from exc


def test_endpoint(payload: EndpointTestPayload) -> EndpointTestOkPayload:
    """Probe an endpoint by GET-ing its models URL. Never raises EndpointError;

    failures are reported in the payload."""
    url = resolve_models_url(payload.base_url)
    start = time.perf_counter()
    try:
        _, response = _fetch_json(url, payload.api_key, payload.timeout_seconds)
    except EndpointError as exc:
        return EndpointTestOkPayload(
            ok=False,
            latency_ms=(time.perf_counter() - start) * 1000,
            error=ErrorPayload(code=exc.code, message=exc.message, retryable=exc.retryable),
        )
    return EndpointTestOkPayload(
        ok=True,
        latency_ms=(time.perf_counter() - start) * 1000,
        server=response.headers.get("server"),
    )


def list_models(payload: ModelsListPayload) -> ModelsListOkPayload:
    """Fetch and parse the OpenAI-style models list. Raises EndpointError."""
    url = resolve_models_url(payload.base_url, payload.model_list_path)
    body, _ = _fetch_json(url, payload.api_key, payload.timeout_seconds)

    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise EndpointError(
            "invalid_response",
            f"response from {url} is not an object with a 'data' list of models",
        )
    models = [
        ModelEntry(id=item["id"], raw=item)
        for item in body["data"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    return ModelsListOkPayload(models=models, fetched_at=utc_now_iso())
