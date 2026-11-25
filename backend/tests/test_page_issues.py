from __future__ import annotations

import importlib

HEADERS = {"Authorization": "Bearer hub-test-key"}


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


def seed(hub):
    with hub.connect() as conn:
        stamp = hub.now()
        conn.execute("INSERT INTO fb_pages(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                     ("p1", "Tin Đà Nẵng", stamp, stamp))
        conn.execute("INSERT INTO fb_pages(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                     ("p2", "Tin Huế", stamp, stamp))
        conn.execute("INSERT INTO fb_sources(id,name,url,source_type,created_at) VALUES(?,?,?,?,?)",
                     ("s1", "Nguồn A", "https://8.8.8.8/rss", "rss", stamp))
        conn.execute("INSERT INTO fb_page_sources(page_id,source_id,created_at) VALUES(?,?,?)",
                     ("p1", "s1", stamp))
        conn.execute("""INSERT INTO fb_source_items
          (id,source_id,item_key,title,item_url,summary,first_seen_at) VALUES(?,?,?,?,?,?,?)""",
          ("i1", "s1", "k1", "Sự kiện tại Đà Nẵng", "https://8.8.8.8/i1",
           "Ban tổ chức đang chuẩn bị.", stamp))


def test_issues_require_key(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        assert client.get("/api/facebook-hub/issues").status_code == 401


def test_issues_collect_content_source_and_page_problems(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    from app import content_processor as cp
    from app import evidence_checker as ec
    from app import source_worker as sw
    with TestClient(main.app) as client:
        seed(hub)
        cp.process_item("i1")
        with hub.connect() as conn:
            draft_id = conn.execute("SELECT id FROM fb_drafts").fetchone()["id"]
            conn.execute("UPDATE fb_drafts SET body=? WHERE id=?",
                         ("Sự kiện diễn ra tại Quảng Nam với nhiều hoạt động.", draft_id))
        ec.check_draft(draft_id)

        def broken(url):
            raise RuntimeError("HTTP 500 từ nguồn")
        for _ in range(3):
            assert sw.poll_source("s1", hub.DB_PATH, broken)["status"] == "error"

        data = client.get("/api/facebook-hub/issues", headers=HEADERS).json()
        by_cat = {}
        for item in data["items"]:
            by_cat.setdefault(item["category"], []).append(item)

        content = by_cat["content"]
        assert any(i["ref_id"] == draft_id and i["severity"] == "warning" for i in content)
        source = by_cat["source"]
        failing = next(i for i in source if i["ref_id"] == "s1")
        assert failing["severity"] == "critical"
        assert "3 lần" in failing["detail"] and "HTTP 500" in failing["detail"]
        # Page 2 has no sources at all.
        assert any(i["page_id"] == "p2" and i["ref_type"] == "page" for i in source)
        # Critical issues sort first.
        assert data["items"][0]["severity"] == "critical"
        assert data["counts"]["critical"] >= 1

        only_p2 = client.get("/api/facebook-hub/issues?page_id=p2", headers=HEADERS).json()
        assert {i["page_id"] for i in only_p2["items"]} == {"p2"}
        assert client.get("/api/facebook-hub/issues?page_id=nope", headers=HEADERS).status_code == 404

        # A successful poll clears the source error.
        rss = (b"<?xml version='1.0'?><rss version='2.0'><channel><item><title>Tin moi</title>"
               b"<link>https://8.8.8.8/n1</link></item></channel></rss>")
        assert sw.poll_source("s1", hub.DB_PATH, lambda url: rss)["status"] == "success"
        data = client.get("/api/facebook-hub/issues?page_id=p1", headers=HEADERS).json()
        assert not any(i["ref_id"] == "s1" for i in data["items"])


def test_token_and_publish_problems_are_reported(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    from app.facebook_publisher import init_publisher_db
    with TestClient(main.app) as client:
        seed(hub)
        init_publisher_db()
        stamp = hub.now()
        with hub.connect() as conn:
            conn.execute("""INSERT INTO fb_meta_page_tokens
              (page_id,meta_page_id,token_ciphertext,connected_at,updated_at,health_status,health_message)
              VALUES(?,?,?,?,?,?,?)""",
              ("p1", "m1", "x", stamp, stamp, "invalid", "Token đã bị thu hồi."))
            conn.execute("""INSERT INTO fb_drafts
              (id,page_id,title,body,confidence,review_status,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?)""",
              ("d9", "p2", "Bài lịch", "Nội dung bài viết đủ dài để lưu.", 80, "approved", stamp, stamp))
            conn.execute("""INSERT INTO fb_publish_schedule
              (id,page_id,draft_id,planned_at,state,created_at,updated_at,publish_error)
              VALUES(?,?,?,?,?,?,?,?)""",
              ("q1", "p2", "d9", stamp, "unknown", stamp, stamp, "Hết thời gian chờ phản hồi."))
        data = client.get("/api/facebook-hub/issues", headers=HEADERS).json()
        fb = [i for i in data["items"] if i["category"] == "facebook"]
        assert fb and fb[0]["severity"] == "critical" and fb[0]["page_id"] == "p1"
        pub = [i for i in data["items"] if i["category"] == "publish"]
        assert pub and pub[0]["severity"] == "critical" and "đăng trùng" in pub[0]["detail"]
