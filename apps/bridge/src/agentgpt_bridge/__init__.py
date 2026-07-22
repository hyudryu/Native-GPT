"""AgentGPT remote backend host (bridge).

Manages GPU workloads (ComfyUI, OpenVoice) on a remote Linux host. Run with::

    AGENTGPT_BRIDGE_TOKEN=<token> python -m agentgpt_bridge --host 0.0.0.0 --port 8443
"""

from __future__ import annotations

__version__ = "0.1.0"
