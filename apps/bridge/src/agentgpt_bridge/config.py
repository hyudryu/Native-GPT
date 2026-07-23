"""Bridge configuration.

Resolved from environment variables at startup. The bridge binds localhost
by default; set ``--host`` to expose it on a network (e.g. Tailscale).
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass


def resolve_token() -> str:
    """Resolve the bridge bearer token.

    Precedence: AGENTGPT_BRIDGE_TOKEN env → generate a random one (printed
    once to stderr at startup).
    """
    env_token = os.environ.get("AGENTGPT_BRIDGE_TOKEN", "").strip()
    if env_token:
        return env_token
    return secrets.token_hex(32)


@dataclass
class BridgeConfig:
    host: str = "127.0.0.1"
    port: int = 8443
    token: str = ""
    enable_comfyui: bool = True
    enable_openvoice: bool = True
    # When True, register the FakeWorkload instead of real GPU workloads.
    # Set via AGENTGPT_BRIDGE_FAKE=1 for local dev / tests.
    use_fake_workloads: bool = False
