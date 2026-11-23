import os
from pathlib import Path

import pytest

from metrics_store import record_campaign
from runtime_profiles import apply_profile
import safe_http


def test_production_profile_fails_closed_without_proxy(monkeypatch):
    monkeypatch.delenv("M3U_OUTBOUND_PROXY", raising=False)
    monkeypatch.delenv("M3U_EGRESS_GUARD", raising=False)
    with pytest.raises(RuntimeError, match="M3U_OUTBOUND_PROXY"):
        apply_profile("production")


def test_production_profile_requires_guard_and_applies_proxy(monkeypatch):
    monkeypatch.setenv("M3U_OUTBOUND_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("M3U_EGRESS_GUARD", "1")
    monkeypatch.delenv("M3U_ALLOW_PRIVATE_NETWORKS", raising=False)
    assert apply_profile("production").name == "production"
    assert os.environ["https_proxy"] == "http://proxy.example:8080"
    assert os.environ["NO_PROXY"] == ""


def test_http_transport_fails_closed_when_active_profile_is_production(monkeypatch):
    monkeypatch.setenv("M3U_PROFILE", "production")
    monkeypatch.delenv("M3U_OUTBOUND_PROXY", raising=False)
    monkeypatch.delenv("M3U_EGRESS_GUARD", raising=False)
    with pytest.raises(RuntimeError, match="M3U_OUTBOUND_PROXY"):
        safe_http.open_public_http("https://public.example.test/guide.xml")


def test_metrics_store_persists_aggregates_without_urls(tmp_path):
    path = Path(tmp_path) / "metrics.sqlite3"
    result = record_campaign(path, "campaign", [
        {"url": "https://provider.example/secret/path", "status": "VALID", "ok": True, "latency_ms": 12.0},
        {"url": "https://provider.example/other", "status": "TIMEOUT", "ok": False, "latency_ms": 20.0},
    ])
    assert result == {"run_id": 1, "total": 2, "succeeded": 1, "p50_ms": 12.0, "p95_ms": 20.0, "providers": 1}
    assert b"provider.example" not in path.read_bytes()
    assert b"secret/path" not in path.read_bytes()
