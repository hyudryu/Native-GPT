"""Shared credential-shaped-content detector (stdlib-only).

Used by the memory and knowledge tools so neither ever persists API keys,
passwords, tokens, or private keys. Detection is regex heuristics over common
credential shapes; there is deliberately no override — callers reject matching
content with the `sensitive_content_rejected` error code.

Loaded by file path from `tools/<id>/tool.py` (no package context).
"""

from __future__ import annotations

import re

# (pattern, human-readable label) pairs, checked in order; the first match
# wins and its label is reported back to the caller.
SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), "private key block"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), "API key (sk-...)"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}\b"), "GitHub token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"(?i)\bapi[_-]?key\s*[:=]\s*['\"]?[^\s'\"]{8,}"), "api_key assignment"),
    (re.compile(r"(?i)\bpassword\s*[:=]\s*['\"]?[^\s'\"]{4,}"), "password assignment"),
    (
        re.compile(r"(?i)\b(?:secret|access[_-]?token|auth[_-]?token)\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{10,}"),
        "secret/token assignment",
    ),
)


def scan_for_secret(content: str) -> str | None:
    """Return the label of the first credential shape found, else None.

    Callers turn a non-None result into their own error type, keeping the
    `sensitive_content_rejected` code consistent across tools.
    """
    if not isinstance(content, str):
        return None
    for pattern, label in SECRET_PATTERNS:
        if pattern.search(content):
            return label
    return None
