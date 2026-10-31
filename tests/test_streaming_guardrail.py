import json

from app.gateway import StreamingRedactor, redact_text, luhn_valid


def test_email_redacted_when_split_across_chunks():
    redactor = StreamingRedactor()
    output = redactor.feed("Email alex.morgan@")
    output += redactor.feed("example.com should be hidden")
    output += redactor.feed("", final=True)
    assert "alex.morgan@example.com" not in output
    assert "[REDACTED]" in output


def test_ssn_redacted_when_split_across_chunks():
    redactor = StreamingRedactor()
    output = redactor.feed("SSN 123-45-")
    output += redactor.feed("6789 is private")
    output += redactor.feed("", final=True)
    assert "123-45-6789" not in output
    assert "[REDACTED]" in output


def test_luhn_valid_card_redacted():
    output = redact_text("Card 4111 1111 1111 1111")
    assert "4111" not in output
    assert "[REDACTED]" in output


def test_invalid_luhn_sequence_not_treated_as_card():
    assert not luhn_valid("4111 1111 1111 1112")
    assert "4111 1111 1111 1112" in redact_text("4111 1111 1111 1112")


def test_ssn_invalid_area_group_serial_not_redacted():
    assert "000-12-1234" in redact_text("000-12-1234")
    assert "123-00-1234" in redact_text("123-00-1234")


def test_stream_endpoint_redacts_sensitive_values(gateway_client):
    response = gateway_client.post("/v1/chat/completions",
        headers={"Authorization": "Bearer viewer-token"},
        json={"model": "demo", "stream": True,
              "messages": [{"role": "user", "content": "test"}]})
    assert response.status_code == 200
    assert "alex.morgan@example.com" not in response.text
    assert "123-45-6789" not in response.text
    assert "4111 1111 1111 1111" not in response.text
    assert response.text.count("[REDACTED]") >= 3
    assert "data: [DONE]" in response.text


def test_stream_requires_stream_true(gateway_client):
    response = gateway_client.post("/v1/chat/completions",
        headers={"Authorization": "Bearer viewer-token"}, json={"stream": False})
    assert response.status_code == 400


def test_stream_requires_auth(gateway_client):
    response = gateway_client.post("/v1/chat/completions", json={"stream": True})
    assert response.status_code == 401


def test_sse_parser_handles_arbitrary_byte_boundaries():
    import asyncio
    from app.gateway import parse_sse_events

    async def source():
        wire = (b'data: {"choices":[{"index":0,"delta":{"content":"hello"}}]}\r\n\r\n'
                b'data: [DONE]\r\n\r\n')
        for byte in wire:
            yield bytes([byte])

    async def collect():
        return [item async for item in parse_sse_events(source())]

    events = asyncio.run(collect())
    assert events[0]["choices"][0]["delta"]["content"] == "hello"
    assert events[1] == "[DONE]"


def test_sse_parser_drops_malformed_json_event():
    import asyncio
    from app.gateway import parse_sse_events

    async def source():
        yield b"data: {not-json}\n\n"
        yield b"data: [DONE]\n\n"

    async def collect():
        return [item async for item in parse_sse_events(source())]

    assert asyncio.run(collect()) == ["[DONE]"]


def test_sse_parser_enforces_event_memory_limit(monkeypatch):
    import asyncio
    import pytest
    from app import gateway

    monkeypatch.setattr(gateway, "MAX_SSE_EVENT_BYTES", 32)
    async def source():
        yield b"data: " + (b"x" * 64)

    async def collect():
        return [item async for item in gateway.parse_sse_events(source())]

    with pytest.raises(gateway.SSEProtocolError):
        asyncio.run(collect())


def test_maximum_length_email_is_redacted_across_stream_boundary():
    local = "a" * 64
    domain = "b" * 63 + "." + "c" * 63 + "." + "d" * 61
    address = local + "@" + domain
    assert len(address) == 254
    redactor = StreamingRedactor()
    first = redactor.feed("prefix " + address[:180])
    second = redactor.feed(address[180:] + " suffix " + ("x" * 300))
    final = redactor.feed("", final=True)
    output = first + second + final
    assert address not in output
    assert "[REDACTED]" in output


def test_redactor_emits_safe_prefix_after_bounded_holdback():
    redactor = StreamingRedactor()
    output = redactor.feed("safe text " + ("x" * 400))
    assert output.startswith("safe text ")
    assert len(redactor._pending) <= 260


def test_streaming_redactor_caps_choice_state():
    import pytest
    from app.gateway import redact_openai_event, SSEProtocolError
    event = {"choices": [{"index": 16, "delta": {"content": "hello"}}]}
    with pytest.raises(SSEProtocolError):
        redact_openai_event(event, {})


def test_stream_rejects_non_sse_upstream_response(gateway_client):
    response = gateway_client.post("/v1/chat/completions",
        headers={"Authorization": "Bearer viewer-token"},
        json={"model": "demo", "stream": True,
              "simulate": "wrong_content_type",
              "messages": [{"role": "user", "content": "test"}]})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "invalid_upstream_content_type"
