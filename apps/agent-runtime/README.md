# agentgpt-runtime

Python agent-runtime sidecar for AgentGPT Desktop. The Rust host spawns this
process and speaks NDJSON over stdin/stdout. Logs go to **stderr only** —
stdout is the protocol channel.

## Run

```bash
uv sync
uv run python -m agentgpt_runtime
```

The process reads one JSON envelope per line from stdin and writes one JSON
envelope per line to stdout. It exits `0` on `runtime.shutdown` or stdin EOF.

## Develop

```bash
uv sync            # installs Python 3.12 + deps (incl. dev group)
uv run pytest -q   # tests (subprocess round-trip + in-process)
uv run ruff check .
```

## Protocol

Envelope (see `packages/protocol-types/schemas/envelope.json`):

```json
{"protocol": "1.0", "type": "runtime.hello", "request_id": "<uuid>",
 "timestamp": "<ISO8601>", "payload": {...}}
```

Responses reuse the request's `request_id`. Phase 0 message set:

| Request            | Response                                                        |
| ------------------ | --------------------------------------------------------------- |
| `runtime.hello`    | `runtime.hello.ok` (runtime name, version, protocol, capabilities) |
| `runtime.health`   | `runtime.health.ok` (status, uptime_seconds, rss_bytes via psutil) |
| `runtime.shutdown` | `runtime.shutdown` echoed with empty payload, then process exits 0 |
| `endpoint.test`    | `endpoint.test.ok` (ok, latency_ms, server?, error?) — failures are reported in-payload with codes `connection_error` / `timeout` / `auth_error` / `http_error` / `invalid_response` |
| `models.list`      | `models.list.ok` (models `[{id, raw}]`, fetched_at); unparseable responses → `error` with code `invalid_response` |

- Unknown message types are ignored (no response, not fatal).
- Malformed JSON / invalid envelopes → `error` with code `bad_request`.
- Wrong `protocol` version → `error` with code `unsupported_protocol`.
- Error payloads: `{code, message, retryable}`.

### Endpoint commands

`endpoint.test` / `models.list` GET the server's models URL: trailing
slashes are stripped from `base_url`; if its path already ends in `/v1`,
`/models` is appended, otherwise `model_list_path` (default `/v1/models`) —
never a duplicated `/v1/v1`. When `api_key` is present it is sent only as an
`Authorization: Bearer` header and never appears in responses, errors, or
logs. `timeout_seconds` defaults to 15.

## Agent SDK

`strands-agents` is a declared dependency but is **lazily imported** (only
when an agent run actually starts, via `server.load_agent_sdk()`), keeping
sidecar startup fast.
