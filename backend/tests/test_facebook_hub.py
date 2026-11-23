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


def test_hub_requires_authentication(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        response = client.get("/api/facebook-hub/dashboard")
    assert response.status_code == 401


def test_page_source_assignment_and_confidence_hold(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer hub-test-key"}
    with TestClient(main.app) as client:
        page = client.post("/api/facebook-hub/pages", headers=headers,
                           json={"name": "Tin tức Đà Nẵng"})
        assert page.status_code == 200
        page_id = page.json()["id"]
        source = client.post("/api/facebook-hub/sources", headers=headers,
                              json={"name": "Nguồn RSS thử nghiệm", "url": "https://example.com/rss", "source_type": "rss"})
        assert source.status_code == 200
        source_id = source.json()["id"]
        assigned = client.post(f"/api/facebook-hub/pages/{page_id}/sources/{source_id}", headers=headers)
        assert assigned.status_code == 200
        listing = client.get(f"/api/facebook-hub/pages/{page_id}/sources", headers=headers)
        assert len(listing.json()["items"]) == 1
        draft = client.post("/api/facebook-hub/drafts", headers=headers, json={
            "page_id": page_id, "title": "Thông báo thử nghiệm", "body": "Nội dung kiểm thử có đủ độ dài để tạo bản nháp.",
            "confidence": 42
        })
        assert draft.status_code == 200
        assert draft.json()["review_status"] == "held"
        dashboard = client.get("/api/facebook-hub/dashboard", headers=headers)
        assert dashboard.json()["held_count"] == 1


def test_auto_publish_locked_until_meta_connection(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer hub-test-key"}
    with TestClient(main.app) as client:
        page = client.post("/api/facebook-hub/pages", headers=headers, json={"name": "Page kiểm tra"})
        page_id = page.json()["id"]
        response = client.patch(f"/api/facebook-hub/pages/{page_id}", headers=headers,
                                json={"auto_publish": True})
    assert response.status_code == 409


def test_page_editorial_profile_can_be_updated_and_read(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer hub-test-key"}
    with TestClient(main.app) as client:
        page = client.post("/api/facebook-hub/pages", headers=headers,
                           json={"name": "Tin tức Đà Nẵng"})
        page_id = page.json()["id"]
        payload = {
            "editorial_tone": "Gần gũi, ngắn gọn",
            "editorial_audience": "Người dân Đà Nẵng",
            "editorial_topics": "Giao thông\\nSự kiện địa phương",
            "editorial_avoid": "Tin đồn chưa xác minh",
            "editorial_guidance": "Ưu tiên thời gian, địa điểm và nguồn chính thức.",
        }
        saved = client.put(f"/api/facebook-hub/pages/{page_id}/editorial-profile",
                           headers=headers, json=payload)
        assert saved.status_code == 200
        profile = client.get(f"/api/facebook-hub/pages/{page_id}/editorial-profile",
                             headers=headers)
        assert profile.status_code == 200
        assert profile.json()["editorial_tone"] == payload["editorial_tone"]
        assert profile.json()["editorial_topics"] == payload["editorial_topics"]
        assert profile.json()["editorial_audience"] == payload["editorial_audience"]


def test_source_trust_profile_can_be_updated(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer hub-test-key"}
    with TestClient(main.app) as client:
        source = client.post("/api/facebook-hub/sources", headers=headers, json={
            "name": "Cổng thông tin chính thức",
            "url": "https://example.gov.vn/rss",
            "source_type": "rss",
        })
        source_id = source.json()["id"]
        response = client.patch(f"/api/facebook-hub/sources/{source_id}/trust",
                                 headers=headers, json={
            "trust_level": "official",
            "publisher_group": "co-quan-a",
            "is_official": True,
            "trust_notes": "Đã kiểm tra trang thuộc cơ quan phát hành.",
        })
        assert response.status_code == 200
        dashboard = client.get("/api/facebook-hub/dashboard", headers=headers)
        saved = next(item for item in dashboard.json()["sources"] if item["id"] == source_id)
        assert saved["trust_level"] == "official"
        assert saved["publisher_group"] == "co-quan-a"
        assert saved["is_official"] == 1 or saved["is_official"] is True
