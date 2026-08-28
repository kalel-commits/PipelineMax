import sys
import hmac
import hashlib
import json
import importlib
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    sys.modules.pop("guardrail_main", None)
    module = importlib.import_module("guardrail_main")

    # Redirect the memory store so tests never touch real persisted state.
    from agents.memory_agent import MemoryAgent

    module.memory_agent.store_path = str(tmp_path / "lexicon.json")

    return TestClient(module.app), module


@pytest.fixture
def demo_client(monkeypatch, tmp_path):
    """No WEBHOOK_SECRET -> demo mode (signatures not enforced)."""
    monkeypatch.delenv("WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("WEBHOOK_SECRET", "")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    sys.modules.pop("guardrail_main", None)
    module = importlib.import_module("guardrail_main")
    module.memory_agent.store_path = str(tmp_path / "lexicon.json")
    return TestClient(module.app), module


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_root_endpoint(app_client):
    client, _ = app_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "PipelineAI GitHub Guardrail"


def test_health_endpoint(app_client):
    client, _ = app_client
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["webhook_signature_enforced"] is True


def test_demo_mode_health_reports_signature_not_enforced(demo_client):
    client, _ = demo_client
    body = client.get("/health").json()
    assert body["webhook_secret_configured"] is False
    assert body["webhook_signature_enforced"] is False


def test_demo_mode_webhook_accepts_unsigned_pull_request(demo_client, monkeypatch):
    client, module = demo_client

    async def fake_handle_pr_event(p):
        return {"verdict": "ALLOW"}

    monkeypatch.setattr(module.webhook_handler, "handle_pr_event", fake_handle_pr_event)
    payload = {
        "action": "opened",
        "pull_request": {"number": 1, "head": {"sha": "abc123", "ref": "feature"}},
        "repository": {"name": "repo", "owner": {"login": "owner"}},
    }
    resp = client.post("/webhook", json=payload)  # no X-Hub-Signature-256 header
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


def test_webhook_rejects_missing_signature(app_client):
    client, _ = app_client
    resp = client.post("/webhook", json={"action": "opened", "pull_request": {}})
    assert resp.status_code == 401


def test_webhook_rejects_invalid_signature(app_client):
    client, _ = app_client
    body = json.dumps({"action": "opened", "pull_request": {}}).encode()
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_webhook_accepts_valid_signature_and_dispatches(app_client, monkeypatch):
    client, module = app_client
    payload = {
        "action": "opened",
        "pull_request": {"number": 1, "head": {"sha": "abc123", "ref": "feature"}},
        "repository": {"name": "repo", "owner": {"login": "owner"}},
    }
    body = json.dumps(payload).encode()
    sig = _sign("test-secret", body)

    dispatched = {}

    async def fake_handle_pr_event(p):
        dispatched["payload"] = p
        return {"verdict": "ALLOW"}

    monkeypatch.setattr(module.webhook_handler, "handle_pr_event", fake_handle_pr_event)

    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert dispatched["payload"]["pull_request"]["number"] == 1


def test_webhook_ignores_irrelevant_events(app_client):
    client, _ = app_client
    payload = {"action": "closed", "pull_request": {"number": 1}}
    body = json.dumps(payload).encode()
    sig = _sign("test-secret", body)
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_webhook_rejects_malformed_json_body(app_client):
    client, _ = app_client
    body = b"{not valid json"
    sig = _sign("test-secret", body)
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
