import importlib
import os


def load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "test-secret-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    import app.main as main
    importlib.reload(main)
    main.initialize_database()
    return main


def test_health_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_app(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_job_creation_requires_authentication(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_app(tmp_path, monkeypatch)
    payload = {
        "product_name": "Kệ úp bát mini",
        "product_benefits": "Gọn bếp và dễ vệ sinh",
        "problem": "Bát đĩa thường để lộn xộn trên bàn",
    }
    with TestClient(main.app) as client:
        response = client.post("/v1/jobs", json=payload)
    assert response.status_code == 401


def test_create_and_read_job(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_app(tmp_path, monkeypatch)
    payload = {
        "product_name": "Kệ úp bát mini",
        "product_benefits": "Gọn bếp và dễ vệ sinh",
        "problem": "Bát đĩa thường để lộn xộn trên bàn",
        "duration_seconds": 40,
    }
    headers = {"Authorization": "Bearer test-secret-key"}
    with TestClient(main.app) as client:
        created = client.post("/v1/jobs", json=payload, headers=headers)
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        status_response = client.get(f"/v1/jobs/{job_id}", headers=headers)
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "queued"


def test_rejects_invalid_duration(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_app(tmp_path, monkeypatch)
    payload = {
        "product_name": "Kệ úp bát mini",
        "product_benefits": "Gọn bếp và dễ vệ sinh",
        "problem": "Bát đĩa thường để lộn xộn trên bàn",
        "duration_seconds": 300,
    }
    headers = {"Authorization": "Bearer test-secret-key"}
    with TestClient(main.app) as client:
        response = client.post("/v1/jobs", json=payload, headers=headers)
    assert response.status_code == 422
