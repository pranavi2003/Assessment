"""Assessment gateway: MCP RBAC proxy, streaming PII guardrail, SQLite model router."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .security import Principal, current_principal

logger = logging.getLogger("ai-gateway")

DATABASE_PATH = os.getenv("DATABASE_PATH", "./gateway_usage.db")
TENANT_TPM_LIMIT = int(os.getenv("TENANT_TPM_LIMIT", "50000"))
RATE_WINDOW_SECONDS = 60
UPSTREAM_TIMEOUT_SECONDS = 3.0
MAX_REQUEST_BYTES = 1_048_576
MAX_SSE_EVENT_BYTES = 1_048_576

MCP_DOWNSTREAM_URL = os.getenv("DOWNSTREAM_MCP_URL", "http://127.0.0.1:8000/mock/mcp")
PRIMARY_MODEL_URL = os.getenv("PRIMARY_MODEL_URL", "http://127.0.0.1:8000/mock/provider/primary-json")
BACKUP_MODEL_URL = os.getenv("BACKUP_MODEL_URL", "http://127.0.0.1:8000/mock/provider/backup-json")
STREAM_MODEL_URL = os.getenv("STREAM_MODEL_URL", "http://127.0.0.1:8000/mock/provider/primary-stream")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(initialize_database, DATABASE_PATH)
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
    timeout = httpx.Timeout(connect=3.0, read=30.0, write=10.0, pool=3.0)
    app.state.http = httpx.AsyncClient(limits=limits, timeout=timeout,
                                       follow_redirects=False)
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="Enterprise AI Gateway Assessment", version="2.0.0",
              lifespan=lifespan)


def jsonrpc_error(request_id: Any, code: int, message: str,
                  *, data: dict[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def response_request_id(payload: Any) -> Any:
    """Return a JSON-RPC-safe id; malformed id values must become null in errors."""
    if not isinstance(payload, dict):
        return None
    request_id = payload.get("id")
    if isinstance(request_id, bool) or isinstance(request_id, (dict, list)):
        return None
    return request_id


def valid_jsonrpc_request(payload: Any) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "request must be a JSON object"
    if payload.get("jsonrpc") != "2.0":
        return False, "jsonrpc must equal '2.0'"
    if not isinstance(payload.get("method"), str) or not payload["method"]:
        return False, "method must be a non-empty string"
    if "params" in payload and not isinstance(payload["params"], (dict, list)):
        return False, "params must be an object or array"
    if "id" in payload and isinstance(payload["id"], (dict, list, bool)):
        return False, "id must be a string, number, or null"
    return True, ""


# ---------------- Task 2: MCP gateway proxy with role-based authorization ----------------
@app.post("/mcp")
async def mcp_proxy(request: Request,
                    principal: Principal = Depends(current_principal)):
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        return JSONResponse(jsonrpc_error(None, -32600, "Request too large"),
                            status_code=413)
    def reject_nonstandard_constant(value: str):
        raise ValueError(f"Non-standard JSON constant: {value}")

    try:
        payload = json.loads(raw, parse_constant=reject_nonstandard_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return JSONResponse(jsonrpc_error(None, -32700, "Parse error"), status_code=400)

    valid, reason = valid_jsonrpc_request(payload)
    if not valid:
        request_id = response_request_id(payload)
        return JSONResponse(jsonrpc_error(request_id, -32600, "Invalid Request",
                                          data={"reason": reason}), status_code=400)

    method = payload["method"]
    params = payload.get("params", {})
    if method in {"tools/list", "tools/call"} and not isinstance(params, dict):
        return JSONResponse(jsonrpc_error(payload.get("id"), -32602,
                                          "Invalid params"), status_code=400)
    if method == "tools/list":
        pass  # Transparent forwarding, preserving the JSON-RPC request body.
    elif method == "tools/call":
        if not isinstance(params, dict):
            return JSONResponse(jsonrpc_error(payload.get("id"), -32602,
                                              "Invalid params"), status_code=400)
        tool_name = params.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            return JSONResponse(jsonrpc_error(payload.get("id"), -32602,
                                              "Invalid params"), status_code=400)
        if tool_name.startswith("admin_") and principal.role != "admin":
            return JSONResponse(jsonrpc_error(payload.get("id"), -32001,
                                              "Unauthorized Tool Call"), status_code=403)
    else:
        return JSONResponse(jsonrpc_error(payload.get("id"), -32601,
                                          "Method not allowed"), status_code=404)

    forward_headers = {
        "content-type": "application/json",
        "accept": request.headers.get("accept", "application/json, text/event-stream"),
    }
    # Preserve the MCP transport/session headers, but never forward the caller's
    # bearer token. Use a gateway-owned downstream credential when configured.
    for header_name in ("mcp-protocol-version", "mcp-session-id", "last-event-id"):
        if header_name in request.headers:
            forward_headers[header_name] = request.headers[header_name]
    downstream_token = os.getenv("MCP_DOWNSTREAM_TOKEN")
    if downstream_token:
        forward_headers["authorization"] = f"Bearer {downstream_token}"

    try:
        upstream = await request.app.state.http.post(
            MCP_DOWNSTREAM_URL,
            content=raw,
            headers=forward_headers,
        )
    except httpx.HTTPError:
        return JSONResponse(jsonrpc_error(payload.get("id"), -32000,
                                          "Downstream MCP service unavailable"), status_code=502)
    except httpx.HTTPError:
        logger.exception("MCP downstream transport failure")
        return JSONResponse(jsonrpc_error(payload.get("id"), -32000,
                                          "Downstream MCP service unavailable"), status_code=502)

    # Pass through JSON-RPC response body/status. Do not trust upstream headers wholesale.
    if "id" not in payload:  # JSON-RPC notification: no response body is permitted.
        return Response(status_code=202)
    media_type = upstream.headers.get("content-type", "application/json")
    response_headers = {
        name: upstream.headers[name]
        for name in ("mcp-session-id", "mcp-protocol-version", "www-authenticate",
                     "retry-after", "last-event-id")
        if name in upstream.headers
    }
    return Response(content=upstream.content, status_code=upstream.status_code,
                    media_type=media_type.split(";", 1)[0], headers=response_headers)


# ---------------- Task 3: stateful streaming PII redaction ----------------
EMAIL_RE = re.compile(
    r"(?<![A-Z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"(?=.{1,254}(?:\s|[,;<>)]|$))"
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+",
    re.IGNORECASE,
)
SSN_RE = re.compile(r"(?<!\d)(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?!\d)")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
REDACTION = "[REDACTED]"
# Longest conventional email address is 254 characters. Keep a little extra for boundaries.
REDACTOR_HOLD_BACK = 260


def luhn_valid(candidate: str) -> bool:
    digits = [int(ch) for ch in candidate if ch.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def redact_text(text: str) -> str:
    """Redact email, plausible US SSN, and Luhn-valid 13-19 digit card candidates."""
    text = EMAIL_RE.sub(REDACTION, text)
    text = SSN_RE.sub(REDACTION, text)
    return CARD_RE.sub(lambda m: REDACTION if luhn_valid(m.group(0)) else m.group(0), text)


class SSEProtocolError(Exception):
    """Raised when an upstream SSE event exceeds the configured safety limit."""


class StreamingRedactor:
    """Bounded-memory redactor that detects sensitive values split across deltas."""
    def __init__(self, hold_back: int = REDACTOR_HOLD_BACK):
        self._pending = ""
        self._hold_back = hold_back

    def feed(self, chunk: str, *, final: bool = False) -> str:
        self._pending += chunk
        if final:
            safe = redact_text(self._pending)
            self._pending = ""
            return safe

        boundary = max(0, len(self._pending) - self._hold_back)
        if boundary == 0:
            return ""

        # If a valid sensitive match spans the cut, retain it in full for the next delta.
        for pattern in (EMAIL_RE, SSN_RE, CARD_RE):
            for match in pattern.finditer(self._pending):
                if match.start() < boundary < match.end():
                    if pattern is not CARD_RE or luhn_valid(match.group(0)):
                        boundary = min(boundary, match.start())
                    break
        if boundary <= 0:
            return ""

        output = redact_text(self._pending[:boundary])
        self._pending = self._pending[boundary:]
        return output


async def parse_sse_events(stream: AsyncIterator[bytes]) -> AsyncIterator[dict[str, Any] | str]:
    """Parse SSE incrementally with a hard per-event memory bound."""
    buffer = bytearray()
    data_lines: list[bytes] = []
    event_size = 0
    async for chunk in stream:
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                if len(buffer) + event_size > MAX_SSE_EVENT_BYTES:
                    raise SSEProtocolError("SSE event exceeded configured limit")
                break
            if newline > MAX_SSE_EVENT_BYTES:
                raise SSEProtocolError("SSE line exceeded configured limit")
            line = bytes(buffer[:newline]).rstrip(b"\r")
            del buffer[:newline + 1]
            event_size += len(line) + 1
            if event_size > MAX_SSE_EVENT_BYTES:
                raise SSEProtocolError("SSE event exceeded configured limit")
            if not line:
                if data_lines:
                    data = b"\n".join(data_lines).strip()
                    data_lines.clear()
                    if data == b"[DONE]":
                        yield "[DONE]"
                    else:
                        try:
                            event = json.loads(data)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            logger.warning("Dropping malformed upstream SSE data event")
                            event_size = 0
                            continue
                        if isinstance(event, dict):
                            yield event
                event_size = 0
                continue
            if line.startswith(b":"):
                continue  # SSE comment keepalive
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip())
            # event/id/retry fields are intentionally not forwarded from untrusted upstream.

    if buffer:
        line = bytes(buffer).rstrip(b"\r")
        event_size += len(line)
        if event_size > MAX_SSE_EVENT_BYTES:
            raise SSEProtocolError("SSE event exceeded configured limit")
        if line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        data = b"\n".join(data_lines).strip()
        if data == b"[DONE]":
            yield "[DONE]"
        else:
            try:
                event = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("Dropping malformed final upstream SSE event")
            else:
                if isinstance(event, dict):
                    yield event


def redact_openai_event(event: dict[str, Any],
                         redactors: dict[int, StreamingRedactor]) -> dict[str, Any]:
    """Redact OpenAI-compatible choices[].delta.content without changing other fields."""
    choices = event.get("choices")
    if not isinstance(choices, list):
        return event
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        index = choice.get("index", 0)
        if not isinstance(index, int):
            index = 0
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            if index < 0 or index >= 16:
                raise SSEProtocolError("Provider exceeded maximum supported choice count")
            redactor = redactors.setdefault(index, StreamingRedactor())
            safe = redactor.feed(content)
            delta["content"] = safe
    return event


@app.post("/v1/chat/completions")
async def guarded_stream(request: Request,
                         principal: Principal = Depends(current_principal)):
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": {"type": "invalid_request_error",
                                        "code": "invalid_json",
                                        "message": "Request body must be valid JSON"}}, status_code=400)
    if not isinstance(body, dict) or body.get("stream") is not True:
        return JSONResponse({"error": {"type": "invalid_request_error",
                                        "code": "stream_required",
                                        "message": "This endpoint requires stream=true"}}, status_code=400)

    try:
        provider_headers = {"content-type": "application/json", "accept": "text/event-stream"}
        provider_api_key = os.getenv("MODEL_PROVIDER_API_KEY")
        if provider_api_key:
            provider_headers["authorization"] = f"Bearer {provider_api_key}"
        upstream_request = request.app.state.http.build_request(
            "POST", STREAM_MODEL_URL, json=body, headers=provider_headers,
        )
        upstream = await request.app.state.http.send(upstream_request, stream=True)
    except httpx.HTTPError:
        return JSONResponse({"error": {"type": "gateway_error",
                                        "code": "upstream_unavailable",
                                        "message": "Model provider is unavailable"}}, status_code=502)

    if upstream.status_code < 200 or upstream.status_code >= 300:
        await upstream.aclose()
        safe_status = 429 if upstream.status_code == 429 else 502
        safe_code = "upstream_rate_limited" if safe_status == 429 else "upstream_error"
        return JSONResponse({"error": {"type": "gateway_error", "code": safe_code,
                                        "message": "Model provider rejected the request"}},
                            status_code=safe_status)
    upstream_type = upstream.headers.get("content-type", "").lower()
    if "text/event-stream" not in upstream_type:
        await upstream.aclose()
        return gateway_error("invalid_upstream_content_type",
                             "Model provider returned an unsupported response type", 502)

    async def stream_response():
        redactors: dict[int, StreamingRedactor] = {}
        try:
            async for parsed in parse_sse_events(upstream.aiter_bytes()):
                if parsed == "[DONE]":
                    for index, redactor in list(redactors.items()):
                        remainder = redactor.feed("", final=True)
                        if remainder:
                            event = {"choices": [{"index": index,
                                                  "delta": {"content": remainder}}]}
                            yield b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n"
                    yield b"data: [DONE]\n\n"
                    return
                if isinstance(parsed, dict):
                    safe_event = redact_openai_event(parsed, redactors)
                    yield b"data: " + json.dumps(safe_event, separators=(",", ":")).encode() + b"\n\n"
            # Flush held text if the provider closes without a [DONE] sentinel.
            for index, redactor in list(redactors.items()):
                remainder = redactor.feed("", final=True)
                if remainder:
                    event = {"choices": [{"index": index, "delta": {"content": remainder}}]}
                    yield b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n\n"
            yield b"data: [DONE]\n\n"
        except (httpx.HTTPError, SSEProtocolError, asyncio.CancelledError):
            # Do not expose provider exceptions or stack traces. Cancellation propagates.
            logger.warning("Streaming provider interrupted or returned an invalid oversized event")
            if not asyncio.current_task().cancelling():
                error = {"error": {"type": "gateway_error", "code": "stream_interrupted",
                                   "message": "Model stream was interrupted"}}
                yield b"data: " + json.dumps(error).encode() + b"\n\n"
                yield b"data: [DONE]\n\n"
            else:
                raise
        finally:
            await upstream.aclose()

    return StreamingResponse(stream_response(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


# ---------------- Task 4: transactional SQLite token sliding-window limiter ----------------
def initialize_database(path: str) -> None:
    if path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS token_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                tokens INTEGER NOT NULL CHECK(tokens > 0),
                created_at REAL NOT NULL
            )
        """)
        connection.execute("CREATE INDEX IF NOT EXISTS idx_token_events_tenant_time "
                           "ON token_events(tenant_id, created_at)")
        connection.commit()


def estimate_tokens(payload: dict[str, Any]) -> int:
    """Conservative fallback estimate; production should use the selected model tokenizer."""
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return max(1, (len(serialized.encode("utf-8")) + 3) // 4)


@dataclass(frozen=True)
class ReservationResult:
    allowed: bool
    used_tokens: int
    remaining_tokens: int
    retry_after_seconds: int
    reservation_id: int | None = None


def _reserve_tokens_sync(path: str, tenant_id: str, tokens: int, limit: int,
                         now: float, window_seconds: int) -> ReservationResult:
    if tokens <= 0:
        raise ValueError("tokens must be positive")
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cutoff = now - window_seconds
        connection.execute("DELETE FROM token_events WHERE created_at <= ?", (cutoff,))
        used_row = connection.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM token_events "
            "WHERE tenant_id = ? AND created_at > ?",
            (tenant_id, cutoff),
        ).fetchone()
        used = int(used_row[0])
        if tokens > limit or used + tokens > limit:
            # Compute the first time enough existing tokens expire to admit this request.
            # The oldest event alone is not sufficient when later events still exceed quota.
            needed_to_free = max(0, used + tokens - limit)
            events = connection.execute(
                "SELECT created_at, tokens FROM token_events "
                "WHERE tenant_id = ? AND created_at > ? ORDER BY created_at ASC",
                (tenant_id, cutoff),
            ).fetchall()
            expired_tokens = 0
            retry_at = now + window_seconds
            for created_at, event_tokens in events:
                expired_tokens += int(event_tokens)
                if expired_tokens >= needed_to_free:
                    retry_at = float(created_at) + window_seconds
                    break
            retry_after = max(1, int(retry_at - now + 0.999))
            connection.execute("COMMIT")  # Keep expired-event eviction even on a rejected reservation.
            return ReservationResult(False, used, max(0, limit - used), retry_after)
        cursor = connection.execute(
            "INSERT INTO token_events(tenant_id, tokens, created_at) VALUES (?, ?, ?)",
            (tenant_id, tokens, now),
        )
        reservation_id = int(cursor.lastrowid)
        connection.execute("COMMIT")
        return ReservationResult(True, used + tokens, limit - used - tokens, 0,
                                 reservation_id)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


async def reserve_tokens(tenant_id: str, tokens: int, *, now: float | None = None,
                         limit: int | None = None) -> ReservationResult:
    return await asyncio.to_thread(
        _reserve_tokens_sync, DATABASE_PATH, tenant_id, tokens,
        TENANT_TPM_LIMIT if limit is None else limit,
        time.time() if now is None else now, RATE_WINDOW_SECONDS,
    )


def _settle_reservation_sync(path: str, reservation_id: int, actual_tokens: int,
                             now: float, window_seconds: int) -> int:
    if actual_tokens < 0:
        raise ValueError("actual_tokens cannot be negative")
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT tenant_id FROM token_events WHERE id = ?", (reservation_id,)
        ).fetchone()
        if row is None:
            connection.execute("ROLLBACK")
            return 0
        tenant_id = row[0]
        if actual_tokens == 0:
            connection.execute("DELETE FROM token_events WHERE id = ?", (reservation_id,))
        else:
            connection.execute("UPDATE token_events SET tokens = ? WHERE id = ?",
                               (actual_tokens, reservation_id))
        used = connection.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM token_events "
            "WHERE tenant_id = ? AND created_at > ?",
            (tenant_id, now - window_seconds),
        ).fetchone()[0]
        connection.execute("COMMIT")
        return max(0, TENANT_TPM_LIMIT - int(used))
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


async def settle_reservation(reservation_id: int, actual_tokens: int) -> int:
    return await asyncio.to_thread(_settle_reservation_sync, DATABASE_PATH,
                                   reservation_id, actual_tokens, time.time(),
                                   RATE_WINDOW_SECONDS)


async def release_reservation(reservation_id: int) -> None:
    await settle_reservation(reservation_id, 0)


async def release_reservation_safely(reservation_id: int) -> None:
    try:
        await release_reservation(reservation_id)
    except (sqlite3.Error, OSError):
        # Keep the reserved charge if persistence is unavailable: fail closed on quota.
        logger.exception("Unable to release failed request reservation")


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = Field(default="primary-model", min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=100_000)
    max_tokens: int = Field(default=512, ge=1, le=8192)


async def post_with_deadline(client: httpx.AsyncClient, url: str,
                             payload: dict[str, Any]) -> httpx.Response:
    # asyncio.timeout enforces an overall wall-clock deadline, not merely an idle read timeout.
    async with asyncio.timeout(UPSTREAM_TIMEOUT_SECONDS):
        return await client.post(url, json=payload)


def gateway_error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": {"type": "gateway_error", "code": code,
                                    "message": message}}, status_code=status_code)


@app.post("/gateway/completions")
async def route_completion(body: CompletionRequest,
                           request: Request,
                           principal: Principal = Depends(current_principal)):
    payload = body.model_dump()
    estimated_tokens = estimate_tokens(payload) + body.max_tokens
    try:
        reservation = await reserve_tokens(principal.tenant_id, estimated_tokens)
    except (sqlite3.Error, OSError):
        logger.exception("Token quota persistence failed")
        return gateway_error("quota_store_unavailable", "Gateway quota service unavailable", 503)

    if not reservation.allowed:
        return JSONResponse(
            {"error": {"type": "rate_limit_error", "code": "tenant_token_limit",
                        "message": "Tenant token-per-minute quota exceeded"}},
            status_code=429,
            headers={"Retry-After": str(reservation.retry_after_seconds),
                     "X-RateLimit-Limit-Tokens": str(TENANT_TPM_LIMIT),
                     "X-RateLimit-Remaining-Tokens": str(reservation.remaining_tokens)},
        )

    assert reservation.reservation_id is not None
    client: httpx.AsyncClient = request.app.state.http
    primary_retryable = False
    try:
        primary = await post_with_deadline(client, PRIMARY_MODEL_URL, payload)
        if primary.status_code == 429:
            primary_retryable = True
        elif 200 <= primary.status_code < 300:
            try:
                result = primary.json()
            except ValueError:
                await release_reservation_safely(reservation.reservation_id)
                return gateway_error("invalid_upstream_response", "Model provider returned an invalid response", 502)
            actual = result.get("usage", {}).get("total_tokens") if isinstance(result, dict) else None
            remaining = reservation.remaining_tokens
            if isinstance(actual, int) and not isinstance(actual, bool) and actual >= 0:
                try:
                    remaining = await settle_reservation(reservation.reservation_id, actual)
                except (sqlite3.Error, OSError):
                    logger.exception("Unable to reconcile provider token usage; reservation retained")
            return JSONResponse({"model_provider": "primary", "result": result},
                                headers={"X-RateLimit-Remaining-Tokens": str(remaining)})
        elif primary.status_code >= 500:
            primary_retryable = True
        else:
            await release_reservation_safely(reservation.reservation_id)
            return gateway_error("upstream_rejected_request", "Model provider rejected the request", 502)
    except asyncio.CancelledError:
        await release_reservation_safely(reservation.reservation_id)
        raise
    except (TimeoutError, httpx.TransportError):
        primary_retryable = True

    if not primary_retryable:
        await release_reservation_safely(reservation.reservation_id)
        return gateway_error("upstream_error", "Primary model provider failed", 502)

    try:
        backup = await post_with_deadline(client, BACKUP_MODEL_URL, payload)
        if 200 <= backup.status_code < 300:
            try:
                result = backup.json()
            except ValueError:
                await release_reservation_safely(reservation.reservation_id)
                return gateway_error("invalid_backup_response", "Backup provider returned an invalid response", 502)
            actual = result.get("usage", {}).get("total_tokens") if isinstance(result, dict) else None
            remaining = reservation.remaining_tokens
            if isinstance(actual, int) and not isinstance(actual, bool) and actual >= 0:
                try:
                    remaining = await settle_reservation(reservation.reservation_id, actual)
                except (sqlite3.Error, OSError):
                    logger.exception("Unable to reconcile backup token usage; reservation retained")
            return JSONResponse({"model_provider": "backup", "result": result},
                                headers={"X-RateLimit-Remaining-Tokens": str(remaining)})
        logger.warning("Backup provider returned HTTP %s", backup.status_code)
    except asyncio.CancelledError:
        await release_reservation_safely(reservation.reservation_id)
        raise
    except (TimeoutError, httpx.TransportError):
        logger.warning("Backup provider timed out or was unreachable")

    await release_reservation_safely(reservation.reservation_id)
    return gateway_error("providers_unavailable", "All configured model providers are unavailable", 503)


# ---------------- Local deterministic mock upstreams for reproducible assessment tests ----------------
@app.post("/mock/mcp")
async def mock_mcp(request: Request):
    payload = await request.json()
    method = payload.get("method")
    if method == "tools/list":
        result = {"tools": [{"name": "get_customer_record"},
                            {"name": "admin_reset_key"}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "Mock tool executed"}]}
    else:
        return JSONResponse(jsonrpc_error(payload.get("id"), -32601, "Method not found"))
    return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}


@app.post("/mock/provider/primary-json")
async def mock_primary_json(request: Request):
    payload = await request.json()
    if payload.get("simulate") == "429":
        return JSONResponse({"error": "rate limited"}, status_code=429)
    if payload.get("simulate") == "500":
        return JSONResponse({"error": "internal mock failure"}, status_code=500)
    if payload.get("simulate") == "timeout":
        await asyncio.sleep(0.15)
    return {"output": "Primary model completed successfully", "usage": {"total_tokens": 15}}


@app.post("/mock/provider/backup-json")
async def mock_backup_json(request: Request):
    return {"output": "Backup model completed successfully", "usage": {"total_tokens": 12}}


@app.post("/mock/provider/primary-stream")
async def mock_primary_stream(request: Request):
    payload = await request.json()
    if payload.get("simulate") == "429":
        return JSONResponse({"error": "rate limited"}, status_code=429)
    if payload.get("simulate") == "wrong_content_type":
        return JSONResponse({"output": "not an SSE stream"}, status_code=200)

    async def events():
        # Test fixture deliberately splits sensitive values between deltas.
        for content in ("Contact alex.morgan@", "example.com; SSN ", "123-45-6789; card ",
                        "4111 1111 1111 1111."):
            event = {"choices": [{"index": 0, "delta": {"content": content}}]}
            yield b"data: " + json.dumps(event).encode() + b"\n\n"
            await asyncio.sleep(0.01)
        yield b"data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
