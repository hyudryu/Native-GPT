"""Wire-contract tests: thinking-mode payloads validate against messages.json,
and the Python models stay in sync with the schema (RunStartPayload is
extra="forbid", so drift fails loudly here)."""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from agentgpt_runtime.protocol import RunStartPayload


def _payload_for(mode: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": "run-1",
        "conversation_id": "conv-1",
        "message_id": "msg-1",
        "prompt": "hi",
        "history": [],
        "model": {"base_url": "http://127.0.0.1:1234", "model_id": "m"},
    }
    if mode != "default":
        payload["thinking_mode"] = mode
    payload.update(extra)
    return payload


def _validate(messages_schema: dict[str, Any], def_name: str, payload: dict[str, Any]) -> None:
    schema = {"$ref": f"#/$defs/{def_name}", "$defs": messages_schema["$defs"]}
    jsonschema.validate(payload, schema)


@pytest.mark.parametrize("mode", ["off", "high", "max", "default"])
def test_run_start_each_thinking_mode_validates(messages_schema: dict[str, Any], mode: str) -> None:
    payload = _payload_for(mode, max_depth="deep")
    _validate(messages_schema, "run.start", payload)
    # ...and the Python model parses the same payload.
    parsed = RunStartPayload.model_validate(payload)
    assert parsed.thinking_mode == ("high" if mode == "default" else mode)
    assert parsed.max_depth == "deep"


def test_run_start_defaults_are_high_and_standard() -> None:
    parsed = RunStartPayload.model_validate(_payload_for("default"))
    assert parsed.thinking_mode == "high"
    assert parsed.max_depth == "standard"


def test_run_start_rejects_bad_mode_and_unknown_field(messages_schema: dict[str, Any]) -> None:
    with pytest.raises(jsonschema.ValidationError):
        _validate(messages_schema, "run.start", _payload_for("bogus"))
    with pytest.raises(ValidationError):
        RunStartPayload.model_validate(_payload_for("off", unknown_field=1))


def test_run_start_model_thinking_params_overrides(messages_schema: dict[str, Any]) -> None:
    payload = _payload_for("off")
    payload["model"]["thinking_off_params"] = {"reasoning_effort": "low"}
    payload["model"]["thinking_high_params"] = {"thinking": {"type": "enabled"}}
    _validate(messages_schema, "run.start", payload)
    parsed = RunStartPayload.model_validate(payload)
    assert parsed.model.thinking_off_params == {"reasoning_effort": "low"}
    assert parsed.model.thinking_high_params == {"thinking": {"type": "enabled"}}


def test_run_orchestration_event_validates(messages_schema: dict[str, Any]) -> None:
    payload = {
        "run_id": "run-1",
        "conversation_id": "conv-1",
        "state": "INVESTIGATE",
        "steps": [
            {"id": "frame", "label": "Framed the problem", "status": "complete"},
            {
                "id": "sp-memory-comparison",
                "label": "Investigating memory comparison",
                "status": "running",
                "detail": {"worker": "worker-2", "tools_used": ["web-search"]},
            },
            {"id": "synthesize", "label": "Final synthesis", "status": "pending"},
        ],
        "budgets": {
            "tokens_used": 48200,
            "token_budget": 120000,
            "elapsed_s": 140.0,
            "time_budget_s": 600,
        },
    }
    _validate(messages_schema, "run.orchestration", payload)


@pytest.mark.parametrize("bad_status", ["queued", "done", ""])
def test_run_orchestration_rejects_bad_step_status(
    messages_schema: dict[str, Any], bad_status: str
) -> None:
    payload = {
        "run_id": "run-1",
        "state": "FRAME",
        "steps": [{"id": "frame", "label": "Framing", "status": bad_status}],
        "budgets": {
            "tokens_used": 0,
            "token_budget": 120000,
            "elapsed_s": 0,
            "time_budget_s": 600,
        },
    }
    with pytest.raises(jsonschema.ValidationError):
        _validate(messages_schema, "run.orchestration", payload)


def test_run_synthesize_now_validates(messages_schema: dict[str, Any]) -> None:
    _validate(messages_schema, "run.synthesize_now", {"run_id": "run-1"})
    _validate(
        messages_schema,
        "run.synthesize_now.ok",
        {"run_id": "run-1", "acknowledged": True},
    )
    with pytest.raises(jsonschema.ValidationError):
        _validate(messages_schema, "run.synthesize_now", {"run_id": "run-1", "extra": 1})


def test_run_completed_decision_record_field(messages_schema: dict[str, Any]) -> None:
    _validate(
        messages_schema,
        "run.completed",
        {
            "run_id": "run-1",
            "usage": {"total_tokens": 42},
            "decision_record": "runs/ct_run-1/decision.json",
        },
    )
    # Without the field (ordinary runs) it still validates.
    _validate(messages_schema, "run.completed", {"run_id": "run-1"})


def test_emitted_orchestration_envelope_matches_schema(messages_schema: dict[str, Any]) -> None:
    """The sidecar's emit helper produces schema-valid payloads."""
    from agentgpt_runtime.protocol import make_run_orchestration

    envelope = make_run_orchestration(
        "req-1",
        "run-1",
        "conv-1",
        state="CRITIQUE",
        steps=[{"id": "critique", "label": "Adversarial critique", "status": "running"}],
        budgets={
            "tokens_used": 100,
            "token_budget": 120000,
            "elapsed_s": 1.5,
            "time_budget_s": 600,
        },
    )
    assert envelope.type == "run.orchestration"
    _validate(messages_schema, "run.orchestration", envelope.payload)
