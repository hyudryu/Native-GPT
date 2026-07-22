"""Remote host status Strands tool — read-only capability check.

Lists configured remote backend hosts and their workloads (ComfyUI, OpenVoice),
so the agent can decide whether generation/TTS is available and which host to
use before calling the side-effecting tools.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from strands import tool

# Load the shared `_lib/bridge_client.py` for calling the desktop server.
_LIB_PATH = Path(__file__).resolve().parent.parent / "_lib" / "bridge_client.py"
_spec = importlib.util.spec_from_file_location("agentgpt_tools_bridge_client", _LIB_PATH)
assert _spec is not None and _spec.loader is not None
_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bridge)


async def check_status() -> dict[str, Any]:
    """Fetch the list of remote hosts and return a standard-schema result dict."""
    try:
        data = await _bridge.api_get("/api/remote-hosts")
    except _bridge.BridgeClientError as exc:
        return {
            "ok": False,
            "summary": "Could not reach the desktop server",
            "data": {},
            "error": {"code": exc.code, "message": exc.message},
        }

    hosts = data.get("hosts", [])
    if not hosts:
        return {
            "ok": True,
            "summary": "No remote hosts configured",
            "data": {
                "hosts": [],
                "capabilities": {
                    "comfyui_generation": {
                        "enabled": False,
                        "status": "no_remote_host",
                        "user_action_available": "Add a remote host in Settings → Remote Hosts",
                    },
                    "openvoice_tts": {
                        "enabled": False,
                        "status": "no_remote_host",
                        "user_action_available": "Add a remote host in Settings → Remote Hosts",
                    },
                },
            },
            "error": None,
        }

    # Build a summary of capabilities.
    host_summaries = []
    for host in hosts:
        workloads = host.get("workloads") or {}
        host_summaries.append(
            {
                "id": host["id"],
                "name": host["name"],
                "base_url": host["base_url"],
                "status": host.get("status", "unknown"),
                "has_token": host.get("has_token", False),
                "workloads": {
                    wid: {"state": w.get("state"), "healthy": w.get("healthy", False)}
                    for wid, w in workloads.items()
                }
                if workloads
                else {},
            }
        )

    # Determine which capabilities are available.
    reachable = [h for h in hosts if h.get("status") == "reachable"]
    comfyui_ok = any(
        h.get("workloads", {}).get("comfyui", {}).get("healthy")
        for h in reachable
    )
    openvoice_ok = any(
        h.get("workloads", {}).get("openvoice", {}).get("healthy")
        for h in reachable
    )

    return {
        "ok": True,
        "summary": f"{len(hosts)} host(s) configured, {len(reachable)} reachable",
        "data": {
            "hosts": host_summaries,
            "capabilities": {
                "comfyui_generation": {
                    "enabled": comfyui_ok,
                    "status": "ready" if comfyui_ok else "no_healthy_host",
                },
                "openvoice_tts": {
                    "enabled": openvoice_ok,
                    "status": "ready" if openvoice_ok else "no_healthy_host",
                },
            },
        },
        "error": None,
    }


@tool
async def remote_host_status() -> dict[str, Any]:
    """Check which remote backend hosts are configured and what they can do.

    Lists all remote hosts, their reachability status, and the workloads they
    offer (ComfyUI for image/video, OpenVoice for TTS). Use this before calling
    comfyui_generate or openvoice_tts to determine if generation is possible.

    Returns:
        `{ok, summary, data: {hosts: [...], capabilities: {comfyui_generation:
        {enabled, status}, openvoice_tts: {enabled, status}}}, error}`.
    """

    return await check_status()


TOOL = remote_host_status
