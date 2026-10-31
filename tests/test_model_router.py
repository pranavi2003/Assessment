import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import gateway


def test_primary_success(gateway_client):
    response = gateway_client.post("/gateway/completions",
        headers={"Authorization": "Bearer viewer-token"},
        json={"prompt": "hello", "max_tokens": 100})
    assert response.status_code == 200
    assert response.json()["model_provider"] == "primary"


def test_primary_429_falls_back_to_backup(gateway_client):
    response = gateway_client.post("/gateway/completions",
        headers={"Authorization": "Bearer viewer-token"},
        json={"prompt": "hello", "max_tokens": 100, "simulate": "429"})
    assert response.status_code == 200
    assert response.json()["model_provider"] == "backup"


def test_primary_5xx_falls_back_to_backup(gateway_client):
    response = gateway_client.post("/gateway/completions",
        headers={"Authorization": "Bearer viewer-token"},
        json={"prompt": "hello", "max_tokens": 100, "simulate": "500"})
    assert response.status_code == 200
    assert response.json()["model_provider"] == "backup"


def test_backup_failure_is_sanitized(gateway_client, monkeypatch):
    from app import gateway as gw
    async def fail(*args, **kwargs):
        raise TimeoutError("secret upstream stack trace")
    monkeypatch.setattr(gw, "post_with_deadline", fail)
    response = gateway_client.post("/gateway/completions",
        headers={"Authorization": "Bearer viewer-token"},
        json={"prompt": "hello", "max_tokens": 100})
    assert response.status_code == 503
    assert "secret upstream stack trace" not in response.text
    assert response.json()["error"]["code"] == "providers_unavailable"


def test_unauthorized_request_is_rejected(gateway_client):
    response = gateway_client.post("/gateway/completions",
                                   json={"prompt": "hello"})
    assert response.status_code == 401


def test_invalid_completion_request_rejected(gateway_client):
    response = gateway_client.post("/gateway/completions",
        headers={"Authorization": "Bearer viewer-token"}, json={"prompt": "", "max_tokens": 0})
    assert response.status_code == 422


def test_token_reservation_sliding_window_and_eviction(tmp_path):
    path = str(tmp_path / "limiter.db")
    gateway.initialize_database(path)
    first = gateway._reserve_tokens_sync(path, "tenant-a", 70, 100, 1000.0, 60)
    assert first.allowed and first.remaining_tokens == 30
    blocked = gateway._reserve_tokens_sync(path, "tenant-a", 31, 100, 1010.0, 60)
    assert not blocked.allowed
    # Event at t=1000 is exactly outside the window at t=1060 and is evicted.
    after_expiry = gateway._reserve_tokens_sync(path, "tenant-a", 90, 100, 1060.0, 60)
    assert after_expiry.allowed and after_expiry.remaining_tokens == 10


def test_tenants_have_independent_quotas(tmp_path):
    path = str(tmp_path / "limiter.db")
    gateway.initialize_database(path)
    a = gateway._reserve_tokens_sync(path, "tenant-a", 80, 100, 1000.0, 60)
    b = gateway._reserve_tokens_sync(path, "tenant-b", 80, 100, 1000.0, 60)
    assert a.allowed and b.allowed


def test_concurrent_reservations_do_not_oversubscribe(tmp_path):
    path = str(tmp_path / "limiter.db")
    gateway.initialize_database(path)
    def reserve(_):
        return gateway._reserve_tokens_sync(path, "tenant-a", 10, 100, 1000.0, 60).allowed
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(reserve, range(20)))
    assert sum(results) == 10
    with sqlite3.connect(path) as connection:
        used = connection.execute("SELECT SUM(tokens) FROM token_events WHERE tenant_id='tenant-a'").fetchone()[0]
    assert used == 100


def test_request_larger_than_quota_rejected(gateway_client, monkeypatch):
    monkeypatch.setattr(gateway, "TENANT_TPM_LIMIT", 10)
    response = gateway_client.post("/gateway/completions",
        headers={"Authorization": "Bearer viewer-token"},
        json={"prompt": "This request cannot fit", "max_tokens": 100})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "tenant_token_limit"


def test_primary_timeout_falls_back_to_backup(gateway_client, monkeypatch):
    monkeypatch.setattr(gateway, "UPSTREAM_TIMEOUT_SECONDS", 0.02)
    response = gateway_client.post("/gateway/completions",
        headers={"Authorization": "Bearer viewer-token"},
        json={"prompt": "hello", "max_tokens": 100, "simulate": "timeout"})
    assert response.status_code == 200
    assert response.json()["model_provider"] == "backup"


def test_retry_after_waits_until_enough_tokens_expire(tmp_path):
    path = str(tmp_path / "limiter.db")
    gateway.initialize_database(path)
    # 60 tokens expire at t=1060; 40 more at t=1070. A 50-token request
    # needs 50 tokens freed, so retry-after must point to the first expiry.
    gateway._reserve_tokens_sync(path, "tenant-a", 60, 100, 1000.0, 60)
    gateway._reserve_tokens_sync(path, "tenant-a", 40, 100, 1010.0, 60)
    blocked = gateway._reserve_tokens_sync(path, "tenant-a", 50, 100, 1020.0, 60)
    assert not blocked.allowed
    assert blocked.retry_after_seconds == 40


def test_successful_provider_usage_reconciles_reservation(gateway_client):
    response = gateway_client.post("/gateway/completions",
        headers={"Authorization": "Bearer viewer-token"},
        json={"prompt": "hello", "max_tokens": 100})
    assert response.status_code == 200
    with sqlite3.connect(gateway.DATABASE_PATH) as connection:
        charged = connection.execute("SELECT SUM(tokens) FROM token_events").fetchone()[0]
    assert charged == 15  # mock primary reports actual usage


def test_backup_usage_reconciles_reservation(gateway_client):
    response = gateway_client.post("/gateway/completions",
        headers={"Authorization": "Bearer viewer-token"},
        json={"prompt": "hello", "max_tokens": 100, "simulate": "429"})
    assert response.status_code == 200
    with sqlite3.connect(gateway.DATABASE_PATH) as connection:
        charged = connection.execute("SELECT SUM(tokens) FROM token_events").fetchone()[0]
    assert charged == 12  # mock backup reports actual usage


def test_failed_providers_release_token_reservation(gateway_client, monkeypatch):
    async def fail(*args, **kwargs):
        raise TimeoutError("provider timeout")
    monkeypatch.setattr(gateway, "post_with_deadline", fail)
    response = gateway_client.post("/gateway/completions",
        headers={"Authorization": "Bearer viewer-token"},
        json={"prompt": "hello", "max_tokens": 100})
    assert response.status_code == 503
    with sqlite3.connect(gateway.DATABASE_PATH) as connection:
        count = connection.execute("SELECT COUNT(*) FROM token_events").fetchone()[0]
    assert count == 0


def test_auth_principals_can_map_to_distinct_tenants(monkeypatch):
    from app.security import authenticate
    monkeypatch.setenv("ADMIN_TENANT_ID", "tenant-admin")
    monkeypatch.setenv("VIEWER_TENANT_ID", "tenant-viewer")
    assert authenticate("Bearer admin-token").tenant_id == "tenant-admin"
    assert authenticate("Bearer viewer-token").tenant_id == "tenant-viewer"
