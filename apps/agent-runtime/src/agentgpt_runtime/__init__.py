"""AgentGPT Desktop agent-runtime sidecar.

Speaks NDJSON over stdin/stdout with the Rust host. Logs go to stderr only;
stdout is reserved exclusively for protocol messages.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
