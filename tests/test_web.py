from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point Model Deck's config directory at a throwaway path for every test
    in this file.

    Not paranoia: these tests originally patched load_state/save_state on
    modeldeck.state, but router.web imports both by value at module load, so
    the patches applied to nothing and the endpoint tests wrote to the real
    ~/Library state.json -- silently rewriting the operator's scout context
    window to a test fixture's 8192. The patch targets are fixed below; this
    fixture is the backstop so the next missed patch costs nothing."""
    monkeypatch.setenv("MODEL_DECK_CONFIG_DIR", str(tmp_path))


@pytest.fixture
def client():
    """TestClient with the router app, presenting itself as loopback so the
    Tailnet allowlist middleware lets it through. TestClient's *default*
    client host is the literal string "testclient", not an address at all --
    which the allowlist correctly rejects, so it has to be set explicitly."""
    from router.main import app
    return TestClient(app, client=("127.0.0.1", 50000))


# ---------------------------------------------------------------------------
# Allowlist middleware
# ---------------------------------------------------------------------------

class TestAllowlist:
    def test_loopback_allowed(self, client):
        res = client.get("/health")
        assert res.status_code == 200

    @patch("router.main._is_allowed_source", return_value=False)
    def test_non_tailnet_rejected(self, mock_check, client):
        # Override to simulate a non-allowed source
        res = client.get("/health")
        assert res.status_code == 403

    @patch("router.main._is_allowed_source", return_value=True)
    def test_tailscale_ip_allowed(self, mock_check, client):
        res = client.get("/health")
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# State endpoints
# ---------------------------------------------------------------------------

class TestStateEndpoints:
    def test_get_state(self, client):
        res = client.get("/api/state")
        assert res.status_code == 200
        data = res.json()
        assert "active" in data
        assert "roles" in data
        assert "planner" in data

    @patch("router.web.save_state")
    @patch("router.web.load_state")
    def test_put_role(self, mock_load, mock_save, client):
        state = {
            "roles": {"scout": {"port": 8000, "model": "old", "context_window": 4096, "depth": 3, "temperature": 0.5, "top_p": 0.9, "top_k": 40}},
            "planner": {},
            "active": {},
            "prompt_overrides": {},
        }
        mock_load.return_value = state
        mock_save.return_value = Path("/tmp/state.json")
        res = client.put("/api/state/roles/scout", json={"context_window": 8192})
        assert res.status_code == 200
        assert res.json()["role"]["context_window"] == 8192

    def test_put_invalid_role(self, client):
        res = client.put("/api/state/roles/invalid", json={"model": "x"})
        assert res.status_code == 400


# ---------------------------------------------------------------------------
# Pipeline control endpoints
# ---------------------------------------------------------------------------

class TestPipelineEndpoints:
    @patch("router.web._pipeline")
    def test_start_pipeline(self, mock_pipeline, client):
        mock_pipeline.start.return_value = {"status": "running", "phase": "scout"}
        res = client.post("/api/pipeline/start", json={"mode": "full", "path": "/tmp/x", "task": "do it", "start_phase": "scout"})
        assert res.status_code == 200
        mock_pipeline.start.assert_called_once_with("full", "/tmp/x", "do it", "scout")

    @patch("router.web._pipeline")
    def test_start_pipeline_error(self, mock_pipeline, client):
        mock_pipeline.start.return_value = {"error": "Path required"}
        res = client.post("/api/pipeline/start", json={"mode": "full", "path": "", "task": "x", "start_phase": "scout"})
        assert res.status_code == 400

    @patch("router.web._pipeline")
    def test_stop_pipeline(self, mock_pipeline, client):
        res = client.post("/api/pipeline/stop")
        assert res.status_code == 200
        mock_pipeline.stop.assert_called_once()

    @patch("router.web._pipeline")
    def test_status_pipeline(self, mock_pipeline, client):
        mock_pipeline.status.return_value = {"current_phase": "builder", "queue": ["auditor"]}
        res = client.get("/api/pipeline/status")
        assert res.status_code == 200
        assert res.json()["current_phase"] == "builder"

    @patch("router.web._pipeline")
    def test_answer_pipeline(self, mock_pipeline, client):
        res = client.post("/api/pipeline/answer", json={"phase": "builder", "answer": "yes"})
        assert res.status_code == 200
        mock_pipeline.answer.assert_called_once_with("builder", "yes")


# ---------------------------------------------------------------------------
# Prompt endpoints
# ---------------------------------------------------------------------------

class TestPromptEndpoints:
    def test_get_prompts(self, client):
        res = client.get("/api/prompts")
        assert res.status_code == 200
        data = res.json()
        assert "scout" in data
        assert "builder" in data

    @patch("router.web.save_state")
    @patch("router.web.load_state")
    def test_put_prompt(self, mock_load, mock_save, client):
        state = {"prompt_overrides": {}, "roles": {}, "planner": {}, "active": {}}
        mock_load.return_value = state
        mock_save.return_value = Path("/tmp/state.json")
        res = client.put("/api/prompts/scout", json={"text": "custom prompt"})
        assert res.status_code == 200

    @patch("router.web.save_state")
    @patch("router.web.load_state")
    def test_delete_prompt(self, mock_load, mock_save, client):
        state = {"prompt_overrides": {"scout": "old"}, "roles": {}, "planner": {}, "active": {}}
        mock_load.return_value = state
        mock_save.return_value = Path("/tmp/state.json")
        res = client.delete("/api/prompts/scout")
        assert res.status_code == 200
        assert "default" in res.json()


# ---------------------------------------------------------------------------
# Secrets endpoints
# ---------------------------------------------------------------------------

class TestSecretsEndpoints:
    @patch("modeldeck.secrets.set_openai_api_key")
    def test_set_key_never_echoes(self, mock_set, client):
        res = client.put("/api/secrets/openai", json={"key": "sk-secret-123"})
        assert res.status_code == 200
        body = res.text
        assert "sk-secret-123" not in body

    @patch("modeldeck.secrets.get_openai_api_key", return_value="sk-x")
    def test_key_status(self, mock_get, client):
        res = client.get("/api/secrets/openai/status")
        assert res.status_code == 200
        assert res.json()["has_key"] is True


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

class TestStaticServing:
    def test_index_html(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers.get("content-type", "")
        assert "Model Deck" in res.text
