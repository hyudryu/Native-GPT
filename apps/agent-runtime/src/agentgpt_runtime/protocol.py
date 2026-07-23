"""Protocol envelope and Phase 0 payload models.

Envelope: {"protocol": "1.0", "type": "...", "request_id": "...",
           "timestamp": "<ISO8601>", "payload": {...}}

Mirrors packages/protocol-types/schemas/envelope.json and messages.json.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import UTC, datetime
from typing import Any, Literal, TextIO

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROTOCOL_VERSION = "1.0"

# Phase 0 request types this runtime answers.
TYPE_HELLO = "runtime.hello"
TYPE_HEALTH = "runtime.health"
TYPE_SHUTDOWN = "runtime.shutdown"

# Endpoint / model-listing request types.
TYPE_ENDPOINT_TEST = "endpoint.test"
TYPE_MODELS_LIST = "models.list"
TYPE_RUN_START = "run.start"
TYPE_RUN_CANCEL = "run.cancel"
TYPE_RUN_APPROVE = "run.approve"
TYPE_RUN_SYNTHESIZE_NOW = "run.synthesize_now"

# Phase 0 response types.
TYPE_HELLO_OK = "runtime.hello.ok"
TYPE_HEALTH_OK = "runtime.health.ok"
TYPE_ENDPOINT_TEST_OK = "endpoint.test.ok"
TYPE_MODELS_LIST_OK = "models.list.ok"
TYPE_RUN_STARTED = "run.started"
TYPE_RUN_CANCELLED = "run.cancelled"
TYPE_RUN_APPROVE_OK = "run.approve.ok"
TYPE_RUN_SYNTHESIZE_NOW_OK = "run.synthesize_now.ok"
# Streaming event types emitted during a run (no response expected).
TYPE_RUN_APPROVAL_NEEDED = "run.approval_needed"
TYPE_RUN_APPROVAL_RESOLVED = "run.approval_resolved"
TYPE_RUN_ORCHESTRATION = "run.orchestration"
TYPE_ERROR = "error"

# Thinking modes (run.start.thinking_mode). High is the default.
THINKING_MODES = ("off", "high", "max")
ThinkingMode = Literal["off", "high", "max"]
# Depth presets for thinking_mode=max (run.start.max_depth).
MAX_DEPTHS = ("quick", "standard", "deep")
MaxDepth = Literal["quick", "standard", "deep"]

_OUTPUT_LOCK = threading.Lock()


class ProtocolError(Exception):
    """Raised when an incoming line cannot be parsed into a valid envelope."""

    def __init__(self, code: str, message: str, request_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id


class Envelope(BaseModel):
    """Base envelope for every message on the wire."""

    model_config = ConfigDict(extra="forbid")

    protocol: str
    type: str
    request_id: str = Field(min_length=1)
    timestamp: str
    sequence: int | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class HelloPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client: str
    client_version: str


class HelloOkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: str
    runtime_version: str
    protocol: str = PROTOCOL_VERSION
    capabilities: list[str] = Field(default_factory=list)


class HealthOkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str  # "ok" | "degraded"
    uptime_seconds: float
    rss_bytes: int = Field(ge=0)


class ErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False


class EndpointTestPayload(BaseModel):
    """Payload of endpoint.test. api_key is a raw key resolved by the host;

    it must never appear in responses or logs."""

    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: str | None = None
    timeout_seconds: int = Field(default=15, ge=1, le=120)
    # Secure by default: only an explicit false disables verification
    # (self-signed/internal CA servers).
    tls_verify: bool = True


class EndpointTestOkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    latency_ms: float | None = None
    server: str | None = None
    error: ErrorPayload | None = None


class ModelsListPayload(BaseModel):
    """Payload of models.list. api_key must never appear in responses or logs."""

    model_config = ConfigDict(extra="forbid")

    base_url: str
    api_key: str | None = None
    model_list_path: str = "/v1/models"
    timeout_seconds: int = Field(default=15, ge=1, le=120)
    tls_verify: bool = True  # see EndpointTestPayload.tls_verify


class ModelEntry(BaseModel):
    id: str
    raw: dict[str, Any] = Field(default_factory=dict)


class ModelsListOkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[ModelEntry]
    fetched_at: str


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str


class RunModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    model_id: str
    api_key: str | None = None
    # Per-endpoint overrides of the thinking-mode request params, forwarded
    # from the provider record by the host (Settings -> Providers). When set,
    # they replace the built-in THINKING_OFF/HIGH profile for this endpoint.
    thinking_off_params: dict[str, Any] | None = None
    thinking_high_params: dict[str, Any] | None = None


class RunStartPayload(BaseModel):
    """Fully resolved chat request from the trusted host process."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    conversation_id: str
    message_id: str
    prompt: str
    history: list[ChatMessage] = Field(default_factory=list)
    system_prompt: str | None = None
    enabled_tools: list[str] = Field(default_factory=list)
    tls_verify: bool = True  # see EndpointTestPayload.tls_verify
    factory_mode: bool = False
    thinking_mode: ThinkingMode = "high"
    max_depth: MaxDepth = "standard"
    model: RunModel


class RunCancelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str


class RunSynthesizeNowPayload(BaseModel):
    """Stop investigating and synthesize partial results (max mode only)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str


class RunApprovePayload(BaseModel):
    """UI decision for a pending approval prompt (human-in-the-loop gate)."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1)
    approved: bool
    reason: str | None = None


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def make_envelope(msg_type: str, request_id: str, payload: BaseModel | dict[str, Any]) -> Envelope:
    if isinstance(payload, BaseModel):
        # exclude_none: optional payload fields are omitted, not null, so the
        # wire message stays valid against the schemas' additionalProperties.
        payload = payload.model_dump(exclude_none=True)
    return Envelope(
        protocol=PROTOCOL_VERSION,
        type=msg_type,
        request_id=request_id,
        timestamp=utc_now_iso(),
        payload=payload,
    )


def make_error(request_id: str, code: str, message: str, retryable: bool = False) -> Envelope:
    return make_envelope(
        TYPE_ERROR,
        request_id,
        ErrorPayload(code=code, message=message, retryable=retryable),
    )


def make_run_orchestration(
    request_id: str,
    run_id: str,
    conversation_id: str,
    *,
    state: str,
    steps: list[dict[str, Any]],
    budgets: dict[str, Any],
) -> Envelope:
    """Structured max-mode progress event (mirrors messages.json run.orchestration).

    ``steps`` items are {id, label, status, detail?} with status in
    pending|running|complete|failed|skipped; ``budgets`` is
    {tokens_used, token_budget, elapsed_s, time_budget_s}. Shapes are built by
    the orchestration package; the envelope stays loosely typed here.
    """
    return make_envelope(
        TYPE_RUN_ORCHESTRATION,
        request_id,
        {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "state": state,
            "steps": steps,
            "budgets": budgets,
        },
    )


def encode(envelope: Envelope, out: TextIO | None = None) -> None:
    """Write one envelope as a single JSON line to stdout and flush.

    stdout is the protocol channel; nothing else may be written to it.
    """
    stream = out if out is not None else sys.stdout
    # exclude_none: "sequence" is only present on streaming events.
    with _OUTPUT_LOCK:
        stream.write(envelope.model_dump_json(exclude_none=True) + "\n")
        stream.flush()


def parse_line(line: str) -> Envelope:
    """Strictly parse one NDJSON line into an Envelope.

    Raises ProtocolError with a wire-level error code on any failure.
    """
    try:
        raw: Any = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError("bad_request", f"malformed JSON: {exc}") from exc

    request_id: str | None = None
    if isinstance(raw, dict) and isinstance(raw.get("request_id"), str):
        request_id = raw["request_id"]

    if not isinstance(raw, dict):
        raise ProtocolError("bad_request", "message must be a JSON object", request_id)

    try:
        envelope = Envelope.model_validate(raw)
    except ValidationError as exc:
        raise ProtocolError("bad_request", f"invalid envelope: {exc}", request_id) from exc

    if envelope.protocol != PROTOCOL_VERSION:
        raise ProtocolError(
            "unsupported_protocol",
            f"unsupported protocol version {envelope.protocol!r}; expected {PROTOCOL_VERSION!r}",
            envelope.request_id,
        )
    return envelope
