from __future__ import annotations

import importlib

HEADERS = {"Authorization": "Bearer hub-test-key"}
GOOD_SUMMARY = "Ban tổ chức thông báo lễ hội pháo hoa sẽ diễn ra tại bờ sông Hàn với nhiều đội tham gia."


def load_hub_app(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "video-test-key")
    monkeypatch.setenv("FACEBOOK_HUB_API_KEY", "hub-test-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "jobs.sqlite3"))
    monkeypatch.setenv("FACEBOOK_HUB_DB", str(tmp_path / "hub.sqlite3"))
    for key in ("AI_API_BASE_URL", "AI_API_KEY", "AI_MODEL"):
        monkeypatch.delenv(key, raising=False)
    import app.facebook_hub as hub
    importlib.reload(hub)
    import app.main as main
    importlib.reload(main)
    return main


def setup_page(client, hub, trust="established"):
    with hub.connect() as conn:
        stamp = hub.now()
        conn.execute("INSERT INTO fb_pages(id,name,created_at,updated_at) VALUES('p1','Tin Đà Nẵng',?,?)",
                     (stamp, stamp))
        conn.execute("""INSERT INTO fb_sources(id,name,url,source_type,trust_level,publisher_group,created_at)
          VALUES('s1','Báo A','https://a.example/rss','rss',?, 'bao-a',?)""", (trust, stamp))
        conn.execute("INSERT INTO fb_page_sources(page_id,source_id,created_at) VALUES('p1','s1',?)", (stamp,))
    times = [f"{h:02d}:00" for h in range(0, 24, 2)]
    assert client.put("/api/facebook-hub/pages/p1/schedule-settings", headers=HEADERS, json={
        "enabled": True, "timezone": "Asia/Ho_Chi_Minh", "posting_times": times,
        "max_posts_per_day": 12, "min_interval_minutes": 15}).status_code == 200
    assert client.put("/api/facebook-hub/pages/p1/automation", headers=HEADERS, json={
        "auto_approve": True, "max_age_hours": 24}).status_code == 200


def add_item(hub, item_id, title, summary):
    from app import content_processor as cp
    with hub.connect() as conn:
        conn.execute("""INSERT INTO fb_source_items(id,source_id,item_key,title,item_url,summary,first_seen_at)
          VALUES(?,?,?,?,?,?,?)""", (item_id, "s1", item_id, title, f"https://a.example/{item_id}",
                                     summary, hub.now()))
    cp.process_item(item_id)
    with hub.connect() as conn:
        return conn.execute("""SELECT d.id FROM fb_drafts d JOIN fb_item_processing p ON p.draft_id=d.id
          WHERE p.item_id=?""", (item_id,)).fetchone()["id"]


def draft_row(hub, draft_id):
    with hub.connect() as conn:
        return conn.execute("SELECT * FROM fb_drafts WHERE id=?", (draft_id,)).fetchone()


def test_clean_trusted_item_is_auto_approved_cleaned_and_scheduled(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    from app import auto_approval as aa
    with TestClient(main.app) as client:
        setup_page(client, hub)
        draft_id = add_item(hub, "i1", "Đà Nẵng chuẩn bị lễ hội pháo hoa", GOOD_SUMMARY)
        result = aa.run_automation()
        assert result["approved"] == 1 and result["scheduled"] == 1
        draft = draft_row(hub, draft_id)
        assert draft["review_status"] == "approved"
        assert draft["title"] == "Đà Nẵng chuẩn bị lễ hội pháo hoa"
        assert "Ghi chú hệ thống" not in draft["body"] and "chưa được xác minh" not in draft["body"]
        assert draft["body"].startswith("Ban tổ chức thông báo")
        assert "Tự duyệt theo luật" in draft["review_reason"]
        with hub.connect() as conn:
            assert conn.execute("SELECT state FROM fb_publish_schedule WHERE draft_id=?",
                                (draft_id,)).fetchone()[0] == "queued"
        decisions = client.get("/api/facebook-hub/automation/decisions", headers=HEADERS).json()["items"]
        assert decisions[0]["decision"] == "approved" and decisions[0]["schedule_state"] == "queued"
        # Running again does not duplicate anything.
        again = aa.run_automation()
        assert again["approved"] == 0 and again["scheduled"] == 0

        # A person pulls the post back: it is unscheduled and never re-approved.
        assert client.patch(f"/api/facebook-hub/drafts/{draft_id}/review", headers=HEADERS,
                            json={"review_status": "held"}).status_code == 200
        with hub.connect() as conn:
            assert conn.execute("SELECT state FROM fb_publish_schedule WHERE draft_id=?",
                                (draft_id,)).fetchone()[0] == "cancelled"
        aa.run_automation()
        aa.run_automation()
        assert draft_row(hub, draft_id)["review_status"] == "held"
        revoked = client.get("/api/facebook-hub/automation/decisions", headers=HEADERS).json()["items"]
        assert revoked[0]["decision"] == "revoked"


def test_untrusted_thin_and_sensitive_items_stay_held(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    from app import auto_approval as aa
    with TestClient(main.app) as client:
        setup_page(client, hub)
        thin = add_item(hub, "i2", "Tin chỉ có tiêu đề", "")
        sensitive = add_item(hub, "i3", "Tai nạn giao thông",
                             "Vụ tai nạn khiến hai người tử vong trên quốc lộ, lực lượng chức năng đang xử lý.")
        aa.run_automation()
        assert draft_row(hub, thin)["review_status"] == "held"
        assert draft_row(hub, sensitive)["review_status"] == "held"
        held = {d["draft_id"]: d for d in client.get(
            "/api/facebook-hub/automation/decisions?decision=held", headers=HEADERS).json()["items"]}
        assert any("chỉ có tiêu đề" in r for r in held[thin]["reasons"])
        assert any("thương vong" in r for r in held[sensitive]["reasons"])

        with hub.connect() as conn:
            conn.execute("UPDATE fb_sources SET trust_level='community' WHERE id='s1'")
        untrusted = add_item(hub, "i4", "Đà Nẵng mở thêm tuyến xe buýt", GOOD_SUMMARY)
        aa.run_automation()
        assert draft_row(hub, untrusted)["review_status"] == "held"


def test_page_keywords_and_opt_out(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    from app import auto_approval as aa
    with TestClient(main.app) as client:
        setup_page(client, hub)
        client.put("/api/facebook-hub/pages/p1/automation", headers=HEADERS, json={
            "auto_approve": True, "max_age_hours": 24, "extra_block_keywords": "pháo hoa"})
        draft_id = add_item(hub, "i5", "Đà Nẵng chuẩn bị lễ hội pháo hoa", GOOD_SUMMARY)
        aa.run_automation()
        assert draft_row(hub, draft_id)["review_status"] == "held"

        client.put("/api/facebook-hub/pages/p1/automation", headers=HEADERS, json={
            "auto_approve": False, "max_age_hours": 24})
        other = add_item(hub, "i6", "Đà Nẵng mở thêm tuyến xe buýt", GOOD_SUMMARY.replace("pháo hoa", "xe buýt"))
        assert aa.run_automation()["pages"] == 0
        assert draft_row(hub, other)["review_status"] == "held"


def test_manual_drafts_never_auto_approved(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    from app import auto_approval as aa
    with TestClient(main.app) as client:
        setup_page(client, hub)
        created = client.post("/api/facebook-hub/drafts", headers=HEADERS, json={
            "page_id": "p1", "title": "Bài tự viết", "body": "Nội dung tự viết đủ dài để lưu bản nháp.",
            "confidence": 40}).json()
        aa.run_automation()
        assert draft_row(hub, created["id"])["review_status"] != "approved"


def test_emergency_stop_and_auto_stop_on_failures(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    from app import auto_approval as aa
    with TestClient(main.app) as client:
        setup_page(client, hub)
        with hub.connect() as conn:
            conn.execute("UPDATE fb_pages SET auto_publish=1 WHERE id='p1'")
        stop = client.post("/api/facebook-hub/automation/stop", headers=HEADERS).json()
        assert stop["pages_auto_publish_off"] == 1
        settings = client.get("/api/facebook-hub/pages/p1/automation", headers=HEADERS).json()
        assert settings["auto_approve"] is False and "khẩn cấp" in settings["stopped_reason"]
        issues = client.get("/api/facebook-hub/issues", headers=HEADERS).json()["items"]
        assert any("đang dừng" in i["title"] for i in issues)

        client.put("/api/facebook-hub/pages/p1/automation", headers=HEADERS, json={
            "auto_approve": True, "max_age_hours": 24})
        draft_ids = [client.post("/api/facebook-hub/drafts", headers=HEADERS, json={
            "page_id": "p1", "title": f"Bài thử {n}", "body": f"Nội dung bài thử số {n} đủ dài để lưu.",
            "confidence": 40}).json()["id"] for n in range(3)]
        stamp = hub.now()
        from app.facebook_publisher import init_publisher_db
        init_publisher_db()
        with hub.connect() as conn:
            for n, draft_id in enumerate(draft_ids):
                conn.execute("""INSERT INTO fb_publish_schedule
                  (id,page_id,draft_id,planned_at,state,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?)""", (f"q{n}", "p1", draft_id, stamp, "failed", stamp, stamp))
        result = aa.run_automation()
        assert result["stopped"] == ["p1"]
        settings = client.get("/api/facebook-hub/pages/p1/automation", headers=HEADERS).json()
        assert settings["auto_approve"] is False and "Tự dừng" in settings["stopped_reason"]
