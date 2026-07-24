"""Thinking-mode request parameters for Off/High modes (design spec §§1-2).

Off and High are real request-level signals: the runtime merges a
provider-appropriate profile into the Strands ``OpenAIModel`` ``params``
dict, which is passed through to the chat-completions request. Because
AgentGPT talks to arbitrary OpenAI-compatible endpoints, profiles are matched
by base_url host and/or model_id prefix, and endpoints that reject the params
(400-class parameter error) are learned once, persisted, and never sent
thinking params again.

Resolution order (both modes):
1. Per-endpoint override from the provider record (``model.thinking_*_params``).
2. Unsupported cache (previously 400'd) -> send nothing.
3. Known profile matched by host/model.
4. No match -> send nothing (non-reasoning models cost nothing).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from agentgpt_runtime.protocol import RunStartPayload
from agentgpt_runtime.tools.registry import repo_root

logger = logging.getLogger(__name__)

# Spec §1.1 — provider-appropriate "do not think" request parameters.
THINKING_OFF_PROFILES: dict[str, dict[str, Any]] = {
    # OpenAI reasoning models (gpt-5 family, o-series); retry with "minimal".
    "openai": {"reasoning_effort": "none"},
    # Anthropic-compatible endpoints.
    "anthropic": {"thinking": {"type": "disabled"}},
    # Qwen / DashScope-style.
    "qwen": {"extra_body": {"enable_thinking": False}},
    # DeepSeek-style.
    "deepseek": {"extra_body": {"thinking": {"type": "disabled"}}},
}

# Spec §2 — request the model's own high-reasoning behavior.
THINKING_HIGH_PROFILES: dict[str, dict[str, Any]] = {
    "openai": {"reasoning_effort": "high"},
    "anthropic": {"thinking": {"type": "enabled", "budget_tokens": 16384}},
    "qwen": {"extra_body": {"enable_thinking": True}},
    "deepseek": {"extra_body": {"thinking": {"type": "enabled"}}},
}

# Retry ladder per mode after a 400-class parameter error, before the
# endpoint is cached as unsupported and the run proceeds without params.
THINKING_OFF_FALLBACK: dict[str, Any] = {"reasoning_effort": "minimal"}

# base_url host substrings -> profile family.
_HOST_FAMILIES: tuple[tuple[str, str], ...] = (
    ("api.openai.com", "openai"),
    ("anthropic", "anthropic"),
    ("dashscope", "qwen"),
    ("deepseek", "deepseek"),
)

# model_id prefixes -> profile family (checked lowercase).
_MODEL_PREFIX_FAMILIES: tuple[tuple[str, str], ...] = (
    ("gpt-5", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("claude", "anthropic"),
    ("qwen", "qwen"),
    ("qwq", "qwen"),
    ("deepseek", "deepseek"),
)


def match_profile_family(base_url: str, model_id: str) -> str | None:
    """Match an endpoint+model to a thinking-profile family, or None."""
    host = base_url.lower()
    for needle, family in _HOST_FAMILIES:
        if needle in host:
            return family
    model = model_id.lower()
    for prefix, family in _MODEL_PREFIX_FAMILIES:
        if model.startswith(prefix):
            return family
    return None


class ThinkingParamsCache:
    """Persisted set of endpoint+model pairs that rejected thinking params.

    Stored at ``app-data/thinking-params-cache.json``; keyed by
    ``base_url + model_id``. Thread-safe for the sidecar's dispatcher/run
    thread split. A corrupt or unreadable file starts empty (fail open: the
    endpoint relearns on its next 400).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or repo_root() / "app-data" / "thinking-params-cache.json"
        self._lock = threading.Lock()
        self._unsupported: dict[str, dict[str, Any]] | None = None

    @staticmethod
    def key(base_url: str, model_id: str) -> str:
        return json.dumps([base_url.rstrip("/"), model_id])

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._unsupported is not None:
            return self._unsupported
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            entries = data.get("unsupported", {})
            self._unsupported = entries if isinstance(entries, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._unsupported = {}
        return self._unsupported

    def is_unsupported(self, base_url: str, model_id: str) -> bool:
        with self._lock:
            return self.key(base_url, model_id) in self._load()

    def mark_unsupported(self, base_url: str, model_id: str, *, reason: str) -> None:
        with self._lock:
            entries = self._load()
            key = self.key(base_url, model_id)
            if key in entries:
                return
            from agentgpt_runtime.protocol import utc_now_iso  # noqa: PLC0415

            entries[key] = {"reason": reason, "recorded_at": utc_now_iso()}
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps({"unsupported": entries}, indent=2), encoding="utf-8"
                )
                tmp.replace(self._path)
            except OSError:
                # Persistence is best-effort; the in-memory entry still
                # protects the rest of this process.
                logger.warning("could not persist thinking-params cache to %s", self._path)
            logger.info(
                "endpoint %s model %s marked unsupported for thinking params: %s",
                base_url,
                model_id,
                reason,
            )


_PROCESS_CACHE: ThinkingParamsCache | None = None
_PROCESS_CACHE_LOCK = threading.Lock()


def process_cache() -> ThinkingParamsCache:
    """One cache per sidecar process (lazily created)."""
    global _PROCESS_CACHE
    with _PROCESS_CACHE_LOCK:
        if _PROCESS_CACHE is None:
            _PROCESS_CACHE = ThinkingParamsCache()
        return _PROCESS_CACHE


def resolve_thinking_params(
    payload: RunStartPayload,
    mode: str,
    cache: ThinkingParamsCache | None = None,
) -> dict[str, Any] | None:
    """The thinking params to merge into the request, or None for a plain one.

    ``mode`` is "off" or "high" ("max" never reaches request construction).
    """
    model = payload.model
    override = (
        model.thinking_off_params if mode == "off" else model.thinking_high_params
    )
    if override is not None:
        return dict(override)
    cache = cache or process_cache()
    if cache.is_unsupported(model.base_url, model.model_id):
        return None
    family = match_profile_family(model.base_url, model.model_id)
    if family is None:
        return None
    profiles = THINKING_OFF_PROFILES if mode == "off" else THINKING_HIGH_PROFILES
    profile = profiles.get(family)
    return dict(profile) if profile else None


def thinking_attempt_ladder(
    payload: RunStartPayload,
    cache: ThinkingParamsCache | None = None,
) -> list[dict[str, Any] | None]:
    """Ordered param sets to try for off/high, ending in a plain request.

    Off: [profile-or-override, {"reasoning_effort": "minimal"}, None].
    High: [profile-or-override, None]. Unsupported or unmatched: [None].
    """
    mode = payload.thinking_mode
    if mode not in ("off", "high"):
        return [None]
    params = resolve_thinking_params(payload, mode, cache)
    if params is None:
        return [None]
    if mode == "off" and params != THINKING_OFF_FALLBACK:
        return [params, dict(THINKING_OFF_FALLBACK), None]
    return [params, None]


def _param_key_names(params: dict[str, Any]) -> set[str]:
    """Candidate key names a 400 error might mention for these params."""
    names: set[str] = set()
    for key, value in params.items():
        names.add(key.lower())
        if isinstance(value, dict):
            names.update(str(k).lower() for k in value)
    return names


def is_thinking_param_error(exc: BaseException, params: dict[str, Any] | None) -> bool:
    """True when `exc` is a 400-class error blaming one of the thinking params.

    Walks the exception chain (Strands may wrap the OpenAI SDK error) looking
    for a 4xx status code, then checks the message names one of the offending
    parameter keys (e.g. "reasoning_effort", "thinking", "enable_thinking").
    """
    if not params:
        return False
    status: int | None = None
    messages: list[str] = []
    current: BaseException | None = exc
    seen = 0
    while current is not None and seen < 5:
        code = getattr(current, "status_code", None) or getattr(current, "status", None)
        if isinstance(code, int):
            status = code
        messages.append(str(current))
        current = current.__cause__ or current.__context__
        seen += 1
    if status is None or not (400 <= status < 500):
        return False
    haystack = " ".join(messages).lower()
    return any(name in haystack for name in _param_key_names(params))


def next_thinking_attempt(
    exc: BaseException,
    ladder: list[dict[str, Any] | None],
    attempt: int,
    payload: RunStartPayload,
    cache: ThinkingParamsCache | None = None,
) -> int | None:
    """Given a failure at ladder[attempt], the next attempt index, or None.

    Only thinking-parameter 400s advance the ladder. Reaching the final
    ``None`` (plain request) persists the endpoint as unsupported.
    """
    current = ladder[attempt]
    if current is None or not is_thinking_param_error(exc, current):
        return None
    nxt = attempt + 1
    if nxt >= len(ladder):
        return None
    if ladder[nxt] is None:
        cache = cache or process_cache()
        cache.mark_unsupported(
            payload.model.base_url,
            payload.model.model_id,
            reason=f"thinking_mode={payload.thinking_mode}: {exc}",
        )
    return nxt
