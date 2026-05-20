"""Tests for the provider metrics module + the /admin/metrics endpoint."""
from __future__ import annotations


def test_metrics_records_success_and_failure(isolated_db):
    from backend.utils import metrics
    metrics.reset_provider()    # full reset

    metrics.record_provider_call("claude", ok=True, latency_ms=120, input_tokens=80, output_tokens=10)
    metrics.record_provider_call("claude", ok=True, latency_ms=140)
    metrics.record_provider_call("claude", ok=False, latency_ms=200, error="network blip")
    metrics.record_provider_call("deepseek", ok=True, latency_ms=90)

    snap = metrics.snapshot()
    by_name = {row["provider"]: row for row in snap["providers"]}
    assert by_name["claude"]["total_calls"] == 3
    assert by_name["claude"]["success_count"] == 2
    assert by_name["claude"]["failure_count"] == 1
    assert by_name["claude"]["last_error"] == "network blip"
    assert by_name["claude"]["total_input_tokens"] == 80
    assert by_name["deepseek"]["total_calls"] == 1
    assert snap["totals"]["calls"] == 4
    assert snap["totals"]["success"] == 3
    assert snap["totals"]["failures"] == 1
    # Percentiles are computed from the recent latencies (single-provider data)
    assert by_name["claude"]["p95_latency_ms"] >= by_name["claude"]["p50_latency_ms"]


def test_metrics_reset_single_provider(isolated_db):
    from backend.utils import metrics
    metrics.reset_provider()
    metrics.record_provider_call("claude", ok=True, latency_ms=10)
    metrics.record_provider_call("openai", ok=True, latency_ms=20)
    assert metrics.reset_provider("claude") == 1
    snap = metrics.snapshot()
    names = {p["provider"] for p in snap["providers"]}
    assert "claude" not in names
    assert "openai" in names


def test_admin_metrics_endpoint_requires_admin(isolated_db):
    from fastapi.testclient import TestClient
    from backend.auth import create_token, hash_password
    from backend.database import User
    from backend.main import app

    db = isolated_db.SessionLocal()
    try:
        u = User(username="notadmin", password_hash=hash_password("secret123"))
        db.add(u)
        db.commit()
        db.refresh(u)
        non_admin_token = create_token(u.id, u.username, False)
    finally:
        db.close()

    client = TestClient(app)
    # No auth → 401
    assert client.get("/admin/metrics").status_code == 401
    # Auth but not admin → 403
    resp = client.get("/admin/metrics", headers={"Authorization": f"Bearer {non_admin_token}"})
    assert resp.status_code == 403


def test_admin_metrics_endpoint_returns_snapshot(isolated_db):
    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.database import User
    from backend.main import app
    from backend.utils import metrics

    db = isolated_db.SessionLocal()
    try:
        admin = db.query(User).filter_by(is_admin=True).first()
        token = create_token(admin.id, admin.username, True)
    finally:
        db.close()

    metrics.reset_provider()
    metrics.record_provider_call("claude", ok=True, latency_ms=50)

    client = TestClient(app)
    resp = client.get("/admin/metrics", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "providers" in body
    assert "dict_cache" in body
    assert any(p["provider"] == "claude" for p in body["providers"]["providers"])
