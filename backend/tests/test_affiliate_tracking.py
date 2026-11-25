from __future__ import annotations

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

HEADERS = {"Authorization": "Bearer hub-test-key"}
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/130 Mobile Safari/537.36"}


def make_page(client, name="Tin tức Đà Nẵng"):
    return client.post("/api/facebook-hub/pages", headers=HEADERS, json={"name": name}).json()["id"]


def test_affiliate_endpoints_require_key(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        assert client.get("/api/facebook-hub/affiliate/summary").status_code == 401
        assert client.post("/api/facebook-hub/affiliate/links", json={}).status_code == 401


def test_only_shopee_https_targets_are_accepted(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        for bad in ("https://evil.example/x", "http://shopee.vn/abc",
                    "https://shopee.vn.evil.example/x", "javascript:alert(1)"):
            res = client.post("/api/facebook-hub/affiliate/links", headers=HEADERS,
                              json={"label": "Thử", "target_url": bad})
            assert res.status_code == 422, bad
        ok = client.post("/api/facebook-hub/affiliate/links", headers=HEADERS,
                         json={"label": "Nồi chiên", "target_url": "https://s.shopee.vn/abc123"})
        assert ok.status_code == 200


def test_short_link_counts_human_clicks_and_skips_bots(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        page_id = make_page(client)
        link = client.post("/api/facebook-hub/affiliate/links", headers=HEADERS, json={
            "label": "Nồi chiên không dầu", "target_url": "https://shopee.vn/product/1/2",
            "page_id": page_id, "code": "noi-chien"}).json()
        assert link["path"] == "/go/noi-chien"

        first = client.get("/go/noi-chien", headers=BROWSER_UA, follow_redirects=False)
        assert first.status_code == 302
        assert first.headers["location"] == "https://shopee.vn/product/1/2"
        assert "noindex" in first.headers["x-robots-tag"]
        client.get("/go/noi-chien", headers=BROWSER_UA, follow_redirects=False)
        client.get("/go/noi-chien", follow_redirects=False,
                   headers={"User-Agent": "facebookexternalhit/1.1"})
        assert client.get("/go/khong-co", follow_redirects=False).status_code == 404

        links = client.get("/api/facebook-hub/affiliate/links", headers=HEADERS).json()["items"]
        item = links[0]
        assert item["clicks"] == 2
        assert item["unique_clicks"] == 1
        assert item["bot_hits"] == 1

        summary = client.get("/api/facebook-hub/affiliate/summary", headers=HEADERS).json()
        assert summary["clicks"] == 2
        page_row = next(p for p in summary["pages"] if p["id"] == page_id)
        assert page_row["clicks"] == 2

        # Disabled links stop redirecting.
        client.patch(f"/api/facebook-hub/affiliate/links/{item['id']}", headers=HEADERS,
                     json={"enabled": False})
        assert client.get("/go/noi-chien", headers=BROWSER_UA,
                          follow_redirects=False).status_code == 404


def test_no_raw_ip_or_user_agent_is_stored(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    import app.facebook_hub as hub
    with TestClient(main.app) as client:
        client.post("/api/facebook-hub/affiliate/links", headers=HEADERS, json={
            "label": "Sản phẩm", "target_url": "https://shopee.vn/x", "code": "sp-1"})
        client.get("/go/sp-1", headers=BROWSER_UA, follow_redirects=False)
    with hub.connect() as conn:
        row = dict(conn.execute("SELECT * FROM fb_link_clicks").fetchone())
    joined = " ".join(str(v) for v in row.values())
    assert "testclient" not in joined and "Mozilla" not in joined


def test_commissions_manual_and_csv_import(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    main = load_hub_app(tmp_path, monkeypatch)
    from datetime import date
    today = date.today().isoformat()
    with TestClient(main.app) as client:
        page_id = make_page(client)
        client.post("/api/facebook-hub/affiliate/links", headers=HEADERS, json={
            "label": "Sản phẩm", "target_url": "https://shopee.vn/x",
            "page_id": page_id, "code": "sp-2"})
        manual = client.post("/api/facebook-hub/affiliate/commissions", headers=HEADERS, json={
            "order_ref": "M1", "order_date": today, "amount_vnd": 15000,
            "status": "approved", "link_code": "sp-2"})
        assert manual.status_code == 200
        csv_text = (
            "order_ref,order_date,amount_vnd,status,link_code\n"
            f"A1,{today},\"12.500\",chờ xử lý,sp-2\n"
            f"A2,{today},8000,đã hủy,\n"
            f"A3,{today},9000,không rõ,\n"
            f"A4,2026/01/01,9000,pending,\n"
        )
        imported = client.post("/api/facebook-hub/affiliate/commissions/import",
                               headers=HEADERS, json={"csv_text": csv_text}).json()
        assert imported["created"] == 2
        assert len(imported["errors"]) == 2
        # Re-import updates instead of duplicating.
        again = client.post("/api/facebook-hub/affiliate/commissions/import",
                            headers=HEADERS, json={"csv_text": csv_text}).json()
        assert again["created"] == 0 and again["updated"] == 2

        bad = client.post("/api/facebook-hub/affiliate/commissions/import", headers=HEADERS,
                          json={"csv_text": "foo,bar\n1,2\n"})
        assert bad.status_code == 422

        summary = client.get("/api/facebook-hub/affiliate/summary", headers=HEADERS).json()
        assert summary["commission_vnd"] == {"pending": 12500, "approved": 15000, "cancelled": 8000}
        page_row = next(p for p in summary["pages"] if p["id"] == page_id)
        assert page_row["commission_pending"] == 12500
        assert page_row["commission_approved"] == 15000
