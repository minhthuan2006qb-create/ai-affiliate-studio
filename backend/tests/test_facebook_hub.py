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

def test_meta_oauth_start_requires_server_configuration(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    for key in ("META_APP_ID", "META_APP_SECRET", "META_REDIRECT_URI", "META_TOKEN_ENCRYPTION_KEY"):
        monkeypatch.delenv(key, raising=False)
    with TestClient(main.app) as client:
        response = client.get("/api/facebook-hub/meta/connect")
    assert response.status_code == 503


def test_meta_oauth_callback_encrypts_page_token_and_disconnects(tmp_path, monkeypatch):
    from urllib.parse import urlparse, parse_qs
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("META_APP_ID", "test-app")
    monkeypatch.setenv("META_APP_SECRET", "test-secret")
    monkeypatch.setenv("META_REDIRECT_URI", "http://localhost:8000/api/facebook-hub/meta/callback")
    monkeypatch.setenv("META_TOKEN_ENCRYPTION_KEY", key)
    import app.facebook_meta as meta
    monkeypatch.setattr(meta, "_exchange_code", lambda *args: "user-token-secret")
    monkeypatch.setattr(meta, "_fetch_granted_permissions",
                        lambda token: {"pages_show_list", "pages_read_engagement", "pages_manage_posts"})
    monkeypatch.setattr(meta, "_fetch_pages", lambda token: [{
        "id": "123456", "name": "Page OAuth Test",
        "access_token": "page-token-secret", "tasks": ["CREATE_CONTENT"],
    }])
    headers = {"Authorization": "Bearer hub-test-key"}
    with TestClient(main.app) as client:
        start = client.get("/api/facebook-hub/meta/connect", follow_redirects=False)
        assert start.status_code == 302
        query = parse_qs(urlparse(start.headers["location"]).query)
        state = query["state"][0]
        assert "pages_manage_posts" in query["scope"][0]
        callback = client.get("/api/facebook-hub/meta/callback",
                              params={"code": "temporary-code", "state": state})
        assert callback.status_code == 200
        payload = callback.json()
        assert len(payload["connected_pages"]) == 1
        assert "page-token-secret" not in callback.text
        page_id = payload["connected_pages"][0]["id"]
        with meta.connect() as conn:
            saved = conn.execute("SELECT token_ciphertext FROM fb_meta_page_tokens WHERE page_id=?",
                                 (page_id,)).fetchone()
            assert saved is not None
            assert saved["token_ciphertext"] != "page-token-secret"
        assert meta.decrypt_token(saved["token_ciphertext"]) == "page-token-secret"
        status = client.get("/api/facebook-hub/meta/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["pages"][0]["connection_status"] == "connected"
        disconnected = client.delete(f"/api/facebook-hub/meta/pages/{page_id}", headers=headers)
        assert disconnected.status_code == 200
        with meta.connect() as conn:
            assert conn.execute("SELECT 1 FROM fb_meta_page_tokens WHERE page_id=?",
                                (page_id,)).fetchone() is None


def test_meta_oauth_state_is_single_use(tmp_path, monkeypatch):
    from urllib.parse import urlparse, parse_qs
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    monkeypatch.setenv("META_APP_ID", "test-app")
    monkeypatch.setenv("META_APP_SECRET", "test-secret")
    monkeypatch.setenv("META_REDIRECT_URI", "http://localhost:8000/api/facebook-hub/meta/callback")
    monkeypatch.setenv("META_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import app.facebook_meta as meta
    monkeypatch.setattr(meta, "_exchange_code", lambda *args: "user-token")
    monkeypatch.setattr(meta, "_fetch_granted_permissions",
                        lambda token: {"pages_show_list", "pages_read_engagement", "pages_manage_posts"})
    monkeypatch.setattr(meta, "_fetch_pages", lambda token: [{
        "id": "789", "name": "Page State Test", "access_token": "page-token",
        "tasks": ["CREATE_CONTENT"],
    }])
    with TestClient(main.app) as client:
        start = client.get("/api/facebook-hub/meta/connect", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        first = client.get("/api/facebook-hub/meta/callback",
                           params={"code": "code", "state": state})
        second = client.get("/api/facebook-hub/meta/callback",
                            params={"code": "code", "state": state})
    assert first.status_code == 200
    assert second.status_code == 400

def test_meta_oauth_callback_rejects_state_from_another_browser(tmp_path, monkeypatch):
    from urllib.parse import urlparse, parse_qs
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    monkeypatch.setenv("META_APP_ID", "test-app")
    monkeypatch.setenv("META_APP_SECRET", "test-secret")
    monkeypatch.setenv("META_REDIRECT_URI", "http://localhost:8000/api/facebook-hub/meta/callback")
    monkeypatch.setenv("META_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    with TestClient(main.app) as client:
        start = client.get("/api/facebook-hub/meta/connect", follow_redirects=False)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        # A second client has no browser-bound state cookie.
        other_browser = TestClient(main.app)
        response = other_browser.get("/api/facebook-hub/meta/callback",
                                     params={"code": "code", "state": state})
    assert response.status_code == 400



def test_approval_with_unsupported_evidence_requires_reviewer_note(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    from app import content_processor as cp
    from app import evidence_checker as ec
    headers = {"Authorization": "Bearer hub-test-key"}
    with TestClient(main.app) as client:
        with hub.connect() as conn:
            stamp = hub.now()
            conn.execute("INSERT INTO fb_pages(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                         ("p1", "Tin tức", stamp, stamp))
            conn.execute("INSERT INTO fb_sources(id,name,url,source_type,created_at) VALUES(?,?,?,?,?)",
                         ("s1", "Nguồn A", "https://s1.example/rss", "rss", stamp))
            conn.execute("INSERT INTO fb_page_sources(page_id,source_id,created_at) VALUES(?,?,?)",
                         ("p1", "s1", stamp))
            conn.execute("""INSERT INTO fb_source_items
              (id,source_id,item_key,title,item_url,summary,first_seen_at)
              VALUES(?,?,?,?,?,?,?)""",
              ("i1", "s1", "k1", "Sự kiện tại Đà Nẵng", "https://s1.example/i1",
               "Ban tổ chức đang chuẩn bị chương trình.", stamp))
        cp.process_item("i1")
        with hub.connect() as conn:
            draft_id = conn.execute("SELECT id FROM fb_drafts").fetchone()["id"]
            conn.execute("UPDATE fb_drafts SET body=? WHERE id=?",
                         ("Sự kiện diễn ra tại Quảng Nam với nhiều hoạt động.", draft_id))
        assert ec.check_draft(draft_id)["status"] == "unsupported"

        listing = client.get("/api/facebook-hub/drafts", headers=headers).json()["items"]
        item = next(x for x in listing if x["id"] == draft_id)
        assert item["evidence_unsupported"] >= 1
        assert item["evidence_contradicted"] == 0

        url = f"/api/facebook-hub/drafts/{draft_id}/review"
        blocked = client.patch(url, headers=headers, json={"review_status": "approved"})
        assert blocked.status_code == 409
        assert "không có trong nguồn" in blocked.json()["detail"]
        short = client.patch(url, headers=headers,
                             json={"review_status": "approved", "review_reason": "ok"})
        assert short.status_code == 409
        with hub.connect() as conn:
            assert conn.execute("SELECT review_status FROM fb_drafts WHERE id=?",
                                (draft_id,)).fetchone()[0] != "approved"

        # Holding or rejecting never needs a note.
        assert client.patch(url, headers=headers, json={"review_status": "held"}).status_code == 200

        ok = client.patch(url, headers=headers, json={
            "review_status": "approved",
            "review_reason": "Đã mở bài gốc, bài gốc ghi rõ địa điểm Quảng Nam.",
        })
        assert ok.status_code == 200
        with hub.connect() as conn:
            assert conn.execute("SELECT review_status FROM fb_drafts WHERE id=?",
                                (draft_id,)).fetchone()[0] == "approved"
            assert conn.execute(
                "SELECT COUNT(*) FROM fb_activity WHERE action='draft.evidence_override'"
            ).fetchone()[0] == 1
