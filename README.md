# Enterprise AI Gateway — AI Field Developer Assessment

A structured, runnable reference implementation for the four assessment tasks:

1. Official MCP Python SDK server over stdio with Pydantic validation.
2. HTTP JSON-RPC MCP gateway with Bearer authentication and role-based tool authorization.
3. OpenAI-compatible SSE streaming proxy with bounded-memory PII redaction.
4. Async model router with a transactional SQLite token sliding-window limiter and primary/backup failover.

> **Scope and honesty:** This is a locally runnable assessment implementation with deterministic mock customer/provider services. It is not a production payment processor, identity provider, enterprise DLP engine, or real model integration. Production-hardening requirements are documented below. No implementation can guarantee passing unknown evaluator tests; the included tests document the behavior actually verified here.

## Architecture

```text
AI Agent / Application
        |
        v
FastAPI AI Gateway
  |-- POST /mcp
  |     |-- Bearer authentication
  |     |-- tools/list transparent forwarding
  |     |-- tools/call role authorization (admin_* requires admin)
  |     `-- downstream MCP HTTP endpoint
  |
  |-- POST /v1/chat/completions
  |     |-- upstream SSE stream
  |     |-- incremental SSE parsing (1 MiB event limit)
  |     |-- per-choice stateful PII redaction
  |     `-- bounded 260-character holdback buffer
  |
  `-- POST /gateway/completions
        |-- token estimate + SQLite reservation
        |-- primary model (3-second wall-clock deadline)
        |-- backup on 429 / timeout / connection / 5xx
        `-- sanitized gateway errors

Separate MCP stdio process:
  MCP client <---- JSON-RPC stdin/stdout ----> MCP SDK server
                                             logs -> stderr
```

## Requirements

- Python 3.11 or newer (`asyncio.timeout` is used for a strict upstream deadline).
- pip and a virtual environment.
- The MCP package is pinned to the official SDK v2 stable line (`mcp[cli]>=2,<3`). This implementation uses the v2 low-level `Server` and `MCPError` APIs; do not silently downgrade to v1 without migrating the server and tests.

## Install and run

```bash
python -m venv .venv
source .venv/bin/activate              # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env                   # Windows: copy .env.example .env
```

Start the HTTP gateway:

```bash
uvicorn app.gateway:app --host 127.0.0.1 --port 8000
```

In another terminal, run the MCP stdio server (the MCP client normally launches this process):

```bash
python -m app.mcp_server
```

The stdio server waits silently for JSON-RPC input. Do not type ordinary shell commands into that terminal while it is running. Stop it with Ctrl+C.

Interactive API documentation: <http://127.0.0.1:8000/docs>

## Task 1 — MCP stdio server

Tools:

- `get_customer_record(customer_id)` — `customer_id` must match `^CUST-[A-Z0-9]{5}$`.
- `trigger_refund(customer_id, amount, reason)` — same customer ID constraint, finite `amount > 0`, and trimmed `reason` length of at least 10 characters.

Pydantic models reject unexpected fields, and the explicitly advertised JSON Schemas set `additionalProperties: false`. Validation failures are surfaced as MCP `Invalid params` (`-32602`) errors. The MCP SDK stdio transport owns stdout for JSON-RPC; application logs are explicitly configured for stderr. No `print()` calls are used by the server.

Validation failures are raised as MCP `MCPError` with JSON-RPC `-32602` (Invalid params), using the SDK v2 protocol-error path. The customer record and refund result are deterministic fixtures. The refund tool does **not** move money. A real side-effecting refund needs authenticated customer ownership, permission checks, an idempotency key, audit logging, transaction state, and a payment-provider adapter.

## Task 2 — MCP security gateway

Endpoint: `POST /mcp`

Demo credentials (development only). Both demo identities share `demo-tenant` by default; set `ADMIN_TENANT_ID` and `VIEWER_TENANT_ID` to separate tenant quotas in a local multi-tenant test:

- `Authorization: Bearer admin-token`
- `Authorization: Bearer viewer-token`

Behavior:

- `tools/list` is forwarded with the original JSON-RPC body and MCP session/protocol headers. A gateway-owned `MCP_DOWNSTREAM_TOKEN` may authenticate the downstream service; the caller’s bearer token is not forwarded.
- `tools/call` is forwarded except `params.name` beginning `admin_` requires the admin role.
- Viewer attempts are intercepted with JSON-RPC error `-32001` and HTTP 403; downstream is not called.
- Invalid JSON returns `-32700`; invalid request `-32600`; invalid params `-32602`; unknown method `-32601`.
- Request body is capped at 1 MiB.

Example:

```bash
curl -i http://127.0.0.1:8000/mcp \
  -H 'Authorization: Bearer viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
```

Expected: HTTP 403 and JSON-RPC error code `-32001`.

## Task 3 — LLM streaming PII guardrail

Endpoint: `POST /v1/chat/completions`

- Requires `stream: true` and a valid Bearer token.
- Parses SSE incrementally, including arbitrary network chunk boundaries and CRLF line endings.
- Handles OpenAI-compatible `choices[].delta.content` text.
- Redacts email addresses, plausible US SSNs, and Luhn-valid 13–19 digit payment-card candidates.
- Maintains a bounded 260-character overlap buffer so values split between deltas are not emitted before they can be recognized.
- Enforces a 1 MiB per-event limit and drops malformed upstream JSON events rather than forwarding unparsed data.
- Closes the upstream response when the client disconnects or the stream completes.

Example:

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Authorization: Bearer viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"model":"demo","stream":true,"messages":[{"role":"user","content":"hello"}]}'
```

The gateway does not forward the caller’s bearer token to the model vendor; configure `MODEL_PROVIDER_API_KEY` for provider-side authentication. The local mock provider deliberately splits an email, SSN, and card number across multiple deltas. The stream should contain `[REDACTED]` and not the original sensitive values.

**Latency trade-off:** The bounded holdback is required to avoid leaking a PII value split across chunks. It adds a bounded delay of up to 260 characters to content that has not yet been proven safe. A production DLP implementation should use a tested streaming recognizer and tune the buffer to its documented maximum pattern length. Regex-based detection is not a complete DLP guarantee.

## Task 4 — SQLite rate limiter and model fallback

Endpoint: `POST /gateway/completions`

```bash
curl -i http://127.0.0.1:8000/gateway/completions \
  -H 'Authorization: Bearer viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Summarize the request","max_tokens":256}'
```

- Reserves estimated input plus requested output tokens per tenant.
- Uses a persistent SQLite file (`gateway_usage.db` by default), WAL mode, an indexed timestamp/tenant query, `BEGIN IMMEDIATE`, and `asyncio.to_thread` so synchronous SQLite work does not block the event loop.
- Expires events outside the rolling 60-second window.
- Calculates `Retry-After` from the earliest set of event expirations sufficient to free the required quota, not merely the oldest event.
- Enforces the configured default 50,000 tokens/minute per tenant.
- Applies a strict 3-second wall-clock deadline to each provider request.
- Falls back to the backup provider on primary 429, timeout, connection failure, or 5xx.
- Returns sanitized, stable gateway errors without upstream stack traces.

Use `"simulate":"429"` or `"simulate":"500"` in the request to exercise fallback against the local mock provider. The test suite monkeypatches a shorter deadline to test timeout failover without waiting three seconds.

**Token accounting note:** The dependency-free estimator uses UTF-8 serialized request length divided by four. This is a conservative approximation, not a model tokenizer. When the provider returns `usage.total_tokens`, the reservation is reconciled to actual usage; when usage is absent, the conservative estimate remains charged. In production, use the selected model's tokenizer and reconcile against provider usage.

## Automated tests

```bash
python -m pytest -v
python -m compileall -q app tests
```

The test suite covers:

- MCP stdio protocol discovery and calls (requires installing the pinned MCP SDK).
- Authentication failures, transparent forwarding, admin/viewer RBAC, JSON-RPC parse/request/params/method errors.
- PII detection across deltas, Luhn validation, invalid SSN ranges, SSE byte-boundary parsing, malformed events, and event-size limits.
- Primary success, 429 and 5xx fallback, timeout fallback, sanitized provider failures, tenant isolation, rolling-window eviction, retry-after accuracy, and concurrent quota reservations.

The MCP stdio integration tests import the SDK directly and are required: if the SDK is missing, test collection fails rather than silently skipping the tests. Install `requirements.txt` in the same Python environment used to run `python -m pytest`.

## Production security and reliability checklist

1. Replace static demo tokens with verified JWT/OAuth. Validate signature, issuer, audience, expiry, scopes, subject, and tenant claim. Never derive a role from an unverified token payload.
2. Do not expose demo credentials or mock routes publicly. Put the service behind TLS; use mTLS/service identity for internal service-to-service traffic.
3. Use a tool allowlist and authorize every tool/resource operation. Prefix matching (`admin_`) is only the assessment's simplified policy.
4. Add request IDs, structured logs, metrics, tracing, audit events, and secret redaction. Never log authorization headers, prompts, or PII.
5. Add bounded request bodies, response limits, provider concurrency limits, circuit breakers, and cancellation propagation.
6. Validate provider response schemas and content types. The mock integration returns JSON; adapt the provider client for the real provider's contract.
7. Use an enterprise DLP engine or vetted streaming recognizer for full PII coverage, including international identifiers, structured tool arguments, and non-text modalities.
8. SQLite is a good single-node durable store for this exercise. For multiple gateway replicas, move quota accounting to a shared transactional database or Redis atomic script with a carefully defined consistency model.
9. Add idempotency and transaction safeguards before connecting `trigger_refund` to a payment system.
10. Configure secrets through a secret manager and set `APP_ENV=production`; the built-in static demo tokens are disabled outside development unless explicitly configured (and still should not be used as a production identity system).

## References

- Official MCP Python SDK current documentation: <https://py.sdk.modelcontextprotocol.io/>
- MCP stdio transport and server execution guidance: <https://py.sdk.modelcontextprotocol.io/run/>
