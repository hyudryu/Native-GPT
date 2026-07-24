from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentgpt_runtime.chat import (
    DEFAULT_SYSTEM_PROMPT,
    GROUNDING_DIRECTIVE,
    ChatRuns,
    activity_from_event,
    approval_allowed_tools,
    build_openai_model,
    openai_base_url,
    resolve_system_prompt,
    strands_messages,
    usage_from_result,
)
from agentgpt_runtime.protocol import RunStartPayload


def test_openai_base_url_accepts_root_or_v1() -> None:
    assert openai_base_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434/v1"
    assert openai_base_url("http://127.0.0.1:11434/v1/") == "http://127.0.0.1:11434/v1"


def test_history_is_translated_without_unknown_roles() -> None:
    history = [
        SimpleNamespace(role="user", content="hello"),
        SimpleNamespace(role="assistant", content="hi"),
        SimpleNamespace(role="system", content="ignored"),
    ]
    assert strands_messages(history) == [
        {"role": "user", "content": [{"text": "hello"}]},
        {"role": "assistant", "content": [{"text": "hi"}]},
    ]


def test_cancel_unknown_run_returns_protocol_error() -> None:
    runs = ChatRuns(lambda _event: None)
    response = runs.cancel("missing", "request-1")
    assert response.type == "error"
    assert response.payload["code"] == "run_not_found"


def test_resolve_approval_unknown_id_returns_false() -> None:
    runs = ChatRuns(lambda _event: None)
    assert runs.resolve_approval("never-pending", approved=True) is False


def test_approval_allowed_tools_gates_only_manifest_flagged() -> None:
    ids = ["calculate", "shell-execute", "delete-file"]
    tools = [
        SimpleNamespace(tool_name="calculate"),
        SimpleNamespace(tool_name="shell_execute"),
        SimpleNamespace(tool_name="delete_file"),
    ]
    manifests = {
        "calculate": {"risk": "read"},
        "shell-execute": {"requires_approval": True},
        "delete-file": {"requires_approval": True},
    }
    assert approval_allowed_tools(ids, tools, manifests) == ["calculate"]


def test_approval_allowed_tools_absent_flag_means_no_gate() -> None:
    ids = ["write-file"]
    # No tool_name attribute: falls back to the id with hyphens as underscores.
    tools = [SimpleNamespace(tool_name=None)]
    manifests = {"write-file": {"requires_approval": False}}
    assert approval_allowed_tools(ids, tools, manifests) == ["write_file"]


def test_usage_is_normalized_from_strands_metrics() -> None:
    result = SimpleNamespace(
        metrics=SimpleNamespace(
            accumulated_usage={"inputTokens": 12, "outputTokens": 8, "totalTokens": 20},
            accumulated_metrics={"latencyMs": 2000},
        )
    )
    assert usage_from_result(result) == {
        "input_tokens": 12,
        "output_tokens": 8,
        "total_tokens": 20,
        "latency_ms": 2000.0,
        "tokens_per_second": 4.0,
    }


def test_tool_use_event_becomes_a_concise_activity_update() -> None:
    assert activity_from_event({"current_tool_use": {"name": "github_search"}}) == {
        "message": "Using github_search",
        "source": "github_search",
    }
    assert activity_from_event({"data": "answer text"}) is None


# --- resolve_system_prompt: grounding directive + hallucination guard ---


def test_resolve_system_prompt_uses_default_without_web_search() -> None:
    payload = _run_payload(enabled_tools=["calculate", "read-file"])
    assert resolve_system_prompt(payload) == DEFAULT_SYSTEM_PROMPT


def test_resolve_system_prompt_appends_grounding_when_web_search_enabled() -> None:
    payload = _run_payload(enabled_tools=["calculate", "web-search", "read-file"])
    assert resolve_system_prompt(payload) == f"{DEFAULT_SYSTEM_PROMPT}\n{GROUNDING_DIRECTIVE}"


def test_resolve_system_prompt_explicit_prompt_wins() -> None:
    payload = _run_payload(
        system_prompt="Custom host prompt.",
        enabled_tools=["web-search"],
    )
    assert resolve_system_prompt(payload) == "Custom host prompt."


def test_resolve_system_prompt_factory_mode_uses_factory_prompt() -> None:
    # Factory runs must NOT get the grounding directive: they expose no normal
    # tools (web-search included), so it would be a false instruction.
    payload = _run_payload(factory_mode=True, enabled_tools=["web-search"])
    from agentgpt_runtime.tools.factory import FACTORY_SYSTEM_PROMPT

    assert resolve_system_prompt(payload) == FACTORY_SYSTEM_PROMPT
    assert GROUNDING_DIRECTIVE not in resolve_system_prompt(payload)


def test_default_prompt_forbids_invented_tools_and_cites_critical_thinking() -> None:
    """The prompt must explicitly deny the hallucinated tools. If a user pastes
    the critical-thinking design doc, the system prompt must override it."""
    assert "Critical Thinking" in DEFAULT_SYSTEM_PROMPT
    assert "agentic loop" in DEFAULT_SYSTEM_PROMPT
    assert "not present in your tool list" in DEFAULT_SYSTEM_PROMPT


def test_grounding_directive_names_web_search_tool() -> None:
    assert "web_search" in GROUNDING_DIRECTIVE
    assert "before you write your answer" in GROUNDING_DIRECTIVE



# --- build_openai_model: tls_verify ---


@pytest.fixture
def _unload_strands() -> None:
    """build_openai_model lazily imports strands; keep it out of sys.modules so
    test_protocol's "not imported at startup" assertion still holds. openai is
    unloaded too: these tests monkeypatch httpx.AsyncClient/openai.AsyncOpenAI,
    and openai._base_client subclasses httpx.AsyncClient AT IMPORT TIME — a
    first import under the monkeypatch would poison that class hierarchy for
    the rest of the process."""
    yield
    import sys

    for name in [
        m
        for m in sys.modules
        if m.split(".")[0] in {"strands", "openai"}
    ]:
        del sys.modules[name]


def _run_payload(**kw: object) -> RunStartPayload:
    base: dict = {
        "run_id": "run-1",
        "conversation_id": "conv-1",
        "message_id": "msg-1",
        "prompt": "hello",
        "model": {"base_url": "https://selfsigned.local", "model_id": "model-1"},
    }
    return RunStartPayload.model_validate({**base, **kw})


@pytest.mark.usefixtures("_unload_strands")
def test_build_openai_model_tls_verify_false_injects_unverified_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, dict] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            captured["httpx"] = kwargs

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured["openai"] = kwargs

    # Import openai BEFORE patching httpx: openai._base_client subclasses
    # httpx.AsyncClient at import time, and a first import under the patch
    # would build that hierarchy on a fake.
    import openai  # noqa: F401

    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)

    model = build_openai_model(_run_payload(tls_verify=False))

    # An unverified httpx client backs a pre-configured OpenAI client, which
    # Strands reuses across requests instead of closing (see build_openai_model).
    assert captured["httpx"]["verify"] is False
    assert captured["openai"]["http_client"] is not None
    assert captured["openai"]["base_url"] == "https://selfsigned.local/v1"
    assert model._custom_client is not None


@pytest.mark.usefixtures("_unload_strands")
def test_build_openai_model_tls_verify_absent_keeps_secure_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_async_client(**kwargs: object) -> None:
        raise AssertionError("no custom http_client may be built when tls_verify is absent")

    # Import strands/openai BEFORE patching httpx: openai._base_client
    # subclasses httpx.AsyncClient at import time, and build_openai_model's
    # lazy import would otherwise execute that class definition against the
    # patched httpx.AsyncClient (a plain function here, which cannot be
    # subclassed).
    import openai  # noqa: F401
    import strands.models.openai  # noqa: F401

    monkeypatch.setattr("httpx.AsyncClient", fail_async_client)

    model = build_openai_model(_run_payload())

    assert model._custom_client is None
    assert "http_client" not in model.client_args
    assert model.client_args["base_url"] == "https://selfsigned.local/v1"
