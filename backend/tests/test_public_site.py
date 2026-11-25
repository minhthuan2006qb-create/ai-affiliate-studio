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


AI_BODY = (
    "Đà Nẵng chuẩn bị lễ hội <b>pháo hoa</b> với nhiều hoạt động.\n\n"
    "Lịch chi tiết đang được cập nhật.\n\n"
    "Nguồn tham khảo (liên kết gốc):\n- Nguồn A: https://news.example/story\n\n"
    "Lưu ý: Bản viết lại bằng AI từ trích yếu RSS; chưa được xác minh độc lập. "
    "Cần người kiểm tra nội dung và nguồn trước khi đăng."
)


def seed(hub, status="approved"):
    with hub.connect() as conn:
        stamp = hub.now()
        conn.execute("INSERT INTO fb_pages(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                     ("p1", "Tin Đà Nẵng", stamp, stamp))
        conn.execute("""INSERT INTO fb_drafts
          (id,page_id,source_name,source_url,title,body,confidence,review_status,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)""",
          ("d1", "p1", "Nguồn A", "https://news.example/story", "Lễ hội pháo hoa <script>",
           AI_BODY, 80, status, stamp, stamp))


def test_only_approved_articles_are_public(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    with TestClient(main.app) as client:
        seed(hub, status="held")
        assert client.get("/site/a/d1").status_code == 404
        assert "Lễ hội" not in client.get("/site/").text
        with hub.connect() as conn:
            conn.execute("UPDATE fb_drafts SET review_status='approved' WHERE id='d1'")
        assert client.get("/site/a/d1").status_code == 200
        assert "Lễ hội" in client.get("/site/").text
        assert "Lễ hội" in client.get("/site/p/p1").text
        # Revoking approval removes it again.
        res = client.patch("/api/facebook-hub/drafts/d1/review", headers=HEADERS,
                           json={"review_status": "held"})
        assert res.status_code == 200
        assert client.get("/site/a/d1").status_code == 404
        # Paused Pages are hidden.
        with hub.connect() as conn:
            conn.execute("UPDATE fb_drafts SET review_status='approved' WHERE id='d1'")
            conn.execute("UPDATE fb_pages SET enabled=0 WHERE id='p1'")
        assert client.get("/site/a/d1").status_code == 404
        assert client.get("/site/p/p1").status_code == 404


def test_article_escapes_html_and_hides_internal_notes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    with TestClient(main.app) as client:
        seed(hub)
        res = client.get("/site/a/d1")
        page = res.text
        assert "<script>" not in page and "&lt;script&gt;" in page
        assert "<b>pháo hoa</b>" not in page
        assert "chưa được xác minh độc lập" not in page
        assert "Cần người kiểm tra" not in page
        assert "default-src 'none'" in res.headers["content-security-policy"]


def test_read_original_links_directly_and_products_are_separate(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    with TestClient(main.app) as client:
        seed(hub)
        no_products = client.get("/site/a/d1").text
        assert "Sản phẩm liên quan" not in no_products

        client.post("/api/facebook-hub/affiliate/links", headers=HEADERS, json={
            "label": "Ống nhòm xem pháo hoa", "target_url": "https://s.shopee.vn/abc",
            "page_id": "p1", "draft_id": "d1", "code": "ong-nhom"})
        page = client.get("/site/a/d1").text
        read_original = page.index('href="https://news.example/story"')
        product_block = page.index("Sản phẩm liên quan")
        # The original article button comes first and links straight to the source.
        assert read_original < product_block
        assert "Đọc bài gốc" in page
        button = page[page.index('class="btn"'):page.index("Đọc bài gốc")]
        assert "/go/" not in button
        assert 'href="/go/ong-nhom"' in page and 'rel="sponsored nofollow noopener"' in page
        assert "Liên kết tiếp thị" in page

        # A disabled product link disappears from the article.
        link_id = client.get("/api/facebook-hub/affiliate/links", headers=HEADERS).json()["items"][0]["id"]
        client.patch(f"/api/facebook-hub/affiliate/links/{link_id}", headers=HEADERS,
                     json={"enabled": False})
        assert "Sản phẩm liên quan" not in client.get("/site/a/d1").text


def test_non_http_source_urls_are_not_linked(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    with TestClient(main.app) as client:
        seed(hub)
        with hub.connect() as conn:
            conn.execute("UPDATE fb_drafts SET source_url='javascript:alert(1)' WHERE id='d1'")
        page = client.get("/site/a/d1").text
        assert "javascript:" not in page
        assert "chưa có liên kết bài gốc" in page


def test_readable_article_urls_and_old_links_redirect(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    from app import public_site as ps
    assert ps.slugify("Đà Nẵng chuẩn bị lễ hội pháo hoa!") == "da-nang-chuan-bi-le-hoi-phao-hoa"
    assert ps.slugify("???") == "bai-viet"
    with TestClient(main.app) as client:
        seed(hub)
        long_id = "3f2a9c1b-7d4e-4a1b-9c2d-123456789abc"
        with hub.connect() as conn:
            conn.execute("UPDATE fb_drafts SET id=?, title=? WHERE id='d1'",
                         (long_id, "Đà Nẵng chuẩn bị lễ hội pháo hoa"))
        canonical = "/site/a/da-nang-chuan-bi-le-hoi-phao-hoa-3f2a9c1b7d"
        assert ps.article_path(long_id, "Đà Nẵng chuẩn bị lễ hội pháo hoa") == canonical
        assert client.get(canonical).status_code == 200
        assert f'href="{canonical}"' in client.get("/site/").text

        old = client.get(f"/site/a/{long_id}", follow_redirects=False)
        assert old.status_code == 301 and old.headers["location"] == canonical
        stale = client.get("/site/a/ten-cu-3f2a9c1b7d", follow_redirects=False)
        assert stale.status_code == 301 and stale.headers["location"] == canonical
        assert client.get("/site/a/da-nang-0000000000").status_code == 404

        # Unapproved drafts stay hidden under every URL form.
        with hub.connect() as conn:
            conn.execute("UPDATE fb_drafts SET review_status='held'")
        assert client.get(canonical, follow_redirects=False).status_code == 404
        assert client.get(f"/site/a/{long_id}", follow_redirects=False).status_code == 404


def test_public_body_drops_candidate_system_notes():
    from app.public_site import public_body
    body = ("Ban tổ chức thông báo lễ hội.\n\n"
            "Nguồn tham khảo: Báo A (https://a.example/1).\n"
            "Ghi chú hệ thống: điểm 60. Đây là bản nháp thu thập thông tin, chưa được xác minh độc lập; "
            "không dùng làm thông báo sự kiện hoặc tự động xuất bản.\n"
            "Nguồn đối chiếu bổ sung: Báo B.")
    assert public_body(body) == "Ban tổ chức thông báo lễ hội."
