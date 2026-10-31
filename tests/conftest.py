from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def gateway_client(tmp_path, monkeypatch):
    from app import gateway

    db_path = str(tmp_path / "test_gateway.sqlite3")
    monkeypatch.setattr(gateway, "DATABASE_PATH", db_path)
    monkeypatch.setattr(gateway, "TENANT_TPM_LIMIT", 50_000)
    monkeypatch.setattr(gateway, "MCP_DOWNSTREAM_URL", "http://testserver/mock/mcp")
    monkeypatch.setattr(gateway, "PRIMARY_MODEL_URL", "http://testserver/mock/provider/primary-json")
    monkeypatch.setattr(gateway, "BACKUP_MODEL_URL", "http://testserver/mock/provider/backup-json")
    monkeypatch.setattr(gateway, "STREAM_MODEL_URL", "http://testserver/mock/provider/primary-stream")

    with TestClient(gateway.app) as client:
        # Replace and close the lifespan-created client so the downstream hop is
        # exercised through the same ASGI application without opening sockets.
        import asyncio
        original_client = gateway.app.state.http
        asyncio.run(original_client.aclose())
        gateway.app.state.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app),
            base_url="http://testserver",
            timeout=httpx.Timeout(5.0),
        )
        yield client
        asyncio.run(gateway.app.state.http.aclose())
