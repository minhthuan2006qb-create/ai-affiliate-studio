from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import importlib


def load_hub_app(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "video-test-key")
    monkeypatch.setenv("FACEBOOK_HUB_API_KEY", "hub-test-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("FACEBOOK_HUB_DB", str(tmp_path / "hub.sqlite3"))
    import app.facebook_hub as hub
    importlib.reload(hub)
    import app.main as main
    importlib.reload(main)
    return main



def _prepare_page(client, monkeypatch, tmp_path):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("META_APP_ID", "test-app")
    monkeypatch.setenv("META_APP_SECRET", "test-secret")
    monkeypatch.setenv("META_REDIRECT_URI", "http://localhost:8000/api/facebook-hub/meta/callback")
    monkeypatch.setenv("META_TOKEN_ENCRYPTION_KEY", key)
    response = client.post("/api/facebook-hub/pages",
                           headers={"Authorization": "Bearer hub-test-key"},
                           json={"name": "Page Health Test"})
    assert response.status_code == 200
    page_id = response.json()["id"]
    import app.facebook_meta as meta
    with meta.connect() as conn:
        conn.execute("""
            INSERT INTO fb_meta_page_tokens
              (page_id,meta_page_id,token_ciphertext,granted_scopes,connected_at,updated_at)
            VALUES(?,?,?,?,?,?)
        """, (page_id, "meta-123", meta.encrypt_token("page-secret-token"),
              "pages_show_list,pages_read_engagement,pages_manage_posts", meta.now(), meta.now()))
    return page_id


def test_token_health_reports_healthy_without_exposing_token(tmp_path, monkeypatch):
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_token_health as health
    monkeypatch.setattr(health, "inspect_token", lambda token: {
        "is_valid": True,
        "expires_at": 0,
        "data_access_expires_at": 0,
        "scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
    })
    headers = {"Authorization": "Bearer hub-test-key"}
    with TestClient(main.app) as client:
        page_id = _prepare_page(client, monkeypatch, tmp_path)
        checked = client.post("/api/facebook-hub/meta/health/check", headers=headers)
        assert checked.status_code == 200
        assert checked.json()["pages"][0]["health_status"] == "healthy"
        status = client.get("/api/facebook-hub/meta/health", headers=headers)
        assert status.json()["pages"][0]["health_status"] == "healthy"
        assert "page-secret-token" not in status.text
        assert status.json()["pages"][0]["page_id"] == page_id


def test_invalid_meta_token_requires_reconnect_and_disables_auto_publish(tmp_path, monkeypatch):
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_token_health as health
    monkeypatch.setattr(health, "inspect_token", lambda token: {
        "is_valid": False, "expires_at": 0, "data_access_expires_at": 0, "scopes": [],
    })
    headers = {"Authorization": "Bearer hub-test-key"}
    with TestClient(main.app) as client:
        page_id = _prepare_page(client, monkeypatch, tmp_path)
        checked = client.post("/api/facebook-hub/meta/health/check", headers=headers)
        assert checked.json()["pages"][0]["health_status"] == "invalid"
        with health.connect() as conn:
            page = conn.execute("SELECT connection_status,auto_publish FROM fb_pages WHERE id=?",
                                (page_id,)).fetchone()
        assert page["connection_status"] == "needs_reconnect"
        assert page["auto_publish"] == 0


def test_token_health_warns_when_expiry_is_within_seven_days(tmp_path, monkeypatch):
    import time
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_token_health as health
    monkeypatch.setattr(health, "inspect_token", lambda token: {
        "is_valid": True,
        "expires_at": int(time.time()) + 2 * 24 * 60 * 60,
        "data_access_expires_at": 0,
        "scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
    })
    headers = {"Authorization": "Bearer hub-test-key"}
    with TestClient(main.app) as client:
        _prepare_page(client, monkeypatch, tmp_path)
        checked = client.post("/api/facebook-hub/meta/health/check", headers=headers)
    assert checked.json()["pages"][0]["health_status"] == "expiring"


def test_token_health_requires_hub_authentication(tmp_path, monkeypatch):
    main = load_hub_app(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        response = client.get("/api/facebook-hub/meta/health")
    assert response.status_code == 401
