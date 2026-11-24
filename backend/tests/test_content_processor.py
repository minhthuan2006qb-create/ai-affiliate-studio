from __future__ import annotations

from app import content_processor as cp
from app import facebook_hub as hub
from app import source_worker as sw


def seed(path):
    hub.DB_PATH = path
    hub.init_hub_db()
    with hub.connect() as conn:
        stamp = hub.now()
        conn.execute("INSERT INTO fb_pages(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                     ("p1", "Page Tin tức", stamp, stamp))
        for sid, name, url in [
            ("s1", "Nguồn A", "https://news-a.example/rss"),
            ("s2", "Nguồn B", "https://news-b.example/rss"),
            ("s3", "Nguồn C", "https://news-c.example/rss"),
        ]:
            conn.execute("INSERT INTO fb_sources(id,name,url,source_type,created_at) VALUES(?,?,?,?,?)",
                         (sid, name, url, "rss", stamp))
            conn.execute("INSERT INTO fb_page_sources(page_id,source_id,created_at) VALUES(?,?,?)",
                         ("p1", sid, stamp))


def add_item(path, item_id, source_id, title, url, summary="Mô tả sự kiện được đăng tải"):
    with hub.connect() as conn:
        conn.execute("""INSERT INTO fb_source_items
          (id,source_id,item_key,title,item_url,summary,first_seen_at)
          VALUES(?,?,?,?,?,?,?)""",
          (item_id, source_id, item_id, title, url, summary, hub.now()))


def test_title_similarity():
    assert cp.similarity("Đà Nẵng tổ chức lễ hội pháo hoa", "Lễ hội pháo hoa Đà Nẵng tổ chức") > 0.45
    assert cp.similarity("Giá vàng hôm nay", "Lịch thi đấu bóng đá") < 0.2


def test_one_source_is_held_and_never_auto_published(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    add_item(path, "i1", "s1", "Đà Nẵng tổ chức lễ hội pháo hoa", "https://news-a.example/a")
    result = cp.process_item("i1")
    assert result["drafts_created"] == 1
    with hub.connect() as conn:
        draft = conn.execute("SELECT * FROM fb_drafts").fetchone()
        assert draft["review_status"] == "held"
        assert "chưa được xác minh độc lập" in draft["body"]


def test_three_distinct_hosts_raise_to_pending_not_published(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    add_item(path, "i1", "s1", "Đà Nẵng tổ chức lễ hội pháo hoa", "https://news-a.example/a")
    add_item(path, "i2", "s2", "Lễ hội pháo hoa Đà Nẵng tổ chức", "https://news-b.example/b")
    add_item(path, "i3", "s3", "Đà Nẵng tổ chức lễ hội pháo hoa", "https://news-c.example/c")
    with hub.connect() as conn:
        for sid, group in [("s1", "publisher-a"), ("s2", "publisher-b"), ("s3", "publisher-c")]:
            conn.execute("UPDATE fb_sources SET trust_level='established', publisher_group=? WHERE id=?",
                         (group, sid))
    result = cp.process_item("i1")
    assert result["drafts_created"] == 1
    with hub.connect() as conn:
        draft = conn.execute("SELECT * FROM fb_drafts").fetchone()
        assert draft["confidence"] == 70
        assert draft["review_status"] == "pending"
        assert draft["review_status"] != "published"


def test_processing_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    add_item(path, "i1", "s1", "Thông tin thử nghiệm về giao thông", "https://news-a.example/a")
    first = cp.process_item("i1")
    second = cp.process_item("i1")
    assert first["drafts_created"] == 1
    assert second["drafts_created"] == 0

def test_matching_items_update_one_candidate_instead_of_duplicate_drafts(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    title = "Đà Nẵng tổ chức lễ hội pháo hoa"
    add_item(path, "i1", "s1", title, "https://news-a.example/a")
    add_item(path, "i2", "s2", "Lễ hội pháo hoa Đà Nẵng tổ chức", "https://news-b.example/b")
    add_item(path, "i3", "s3", title, "https://news-c.example/c")
    cp.process_item("i1")
    cp.process_item("i2")
    cp.process_item("i3")
    with hub.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fb_drafts").fetchone()[0] == 1
        draft = conn.execute("SELECT * FROM fb_drafts").fetchone()
        assert draft["confidence"] == 70
        assert "news-b.example" in draft["body"]
        assert "news-c.example" in draft["body"]


def test_conflicting_numeric_claims_force_candidate_to_held(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    add_item(path, "i1", "s1", "Đà Nẵng tổ chức sự kiện pháo hoa", "https://news-a.example/a",
             "Sự kiện dự kiến đón 12 người tham dự.")
    add_item(path, "i2", "s2", "Sự kiện pháo hoa tại Đà Nẵng", "https://news-b.example/b",
             "Ban tổ chức cho biết có 15 người tham dự.")
    add_item(path, "i3", "s3", "Đà Nẵng tổ chức sự kiện pháo hoa", "https://news-c.example/c",
             "Sự kiện có 12 người tham dự.")
    with hub.connect() as conn:
        for sid, group in [("s1", "publisher-a"), ("s2", "publisher-b"), ("s3", "publisher-c")]:
            conn.execute("UPDATE fb_sources SET trust_level='established', publisher_group=? WHERE id=?",
                         (group, sid))
    cp.process_item("i1")
    with hub.connect() as conn:
        draft = conn.execute("SELECT * FROM fb_drafts").fetchone()
        assert draft["review_status"] == "held"
        assert "mâu thuẫn" in draft["review_reason"].lower()


def test_old_published_news_is_held_even_with_multiple_trusted_sources(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    add_item(path, "i1", "s1", "Đà Nẵng tổ chức lễ hội pháo hoa", "https://news-a.example/a")
    add_item(path, "i2", "s2", "Lễ hội pháo hoa Đà Nẵng tổ chức", "https://news-b.example/b")
    add_item(path, "i3", "s3", "Đà Nẵng tổ chức lễ hội pháo hoa", "https://news-c.example/c")
    old_date = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    with hub.connect() as conn:
        for sid, group in [("s1", "publisher-a"), ("s2", "publisher-b"), ("s3", "publisher-c")]:
            conn.execute("UPDATE fb_sources SET trust_level='established', publisher_group=? WHERE id=?",
                         (group, sid))
        conn.execute("UPDATE fb_source_items SET published_at=? WHERE id='i1'", (old_date,))
    result = cp.process_item("i1")
    assert result["results"][0]["status"] == "held"
    with hub.connect() as conn:
        draft = conn.execute("SELECT * FROM fb_drafts").fetchone()
        assert draft["review_status"] == "held"
        assert "tin đã cũ" in draft["review_reason"].lower()


def test_future_dated_news_is_held(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    add_item(path, "i1", "s1", "Thông báo sự kiện Đà Nẵng", "https://news-a.example/a")
    future_date = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with hub.connect() as conn:
        conn.execute("UPDATE fb_source_items SET published_at=? WHERE id='i1'", (future_date,))
    result = cp.process_item("i1")
    assert result["results"][0]["status"] == "held"



def test_approved_source_url_with_tracking_params_is_blocked_on_same_page(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    first = hub.create_draft(hub.DraftCreate(
        page_id="p1", title="Lịch bắn pháo hoa Đà Nẵng 2026",
        source_url="https://example.com/events/fireworks?utm_source=facebook&fbclid=abc",
        body="Lịch bắn pháo hoa Đà Nẵng năm 2026 được công bố với nhiều đêm trình diễn.",
        confidence=90))
    hub.review_draft(first["id"], hub.DraftReview(review_status="approved"))
    second = hub.create_draft(hub.DraftCreate(
        page_id="p1", title="Lịch bắn pháo hoa Đà Nẵng",
        source_url="https://example.com/events/fireworks?utm_campaign=share",
        body="Thông tin lịch bắn pháo hoa Đà Nẵng năm 2026.",
        confidence=95))
    assert second["review_status"] == "held"
    assert "trùng nội dung" in second["review_reason"].lower()


def test_near_duplicate_approved_content_is_blocked_on_same_page(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    first = hub.create_draft(hub.DraftCreate(
        page_id="p1", title="Đà Nẵng tổ chức lễ hội pháo hoa quốc tế",
        source_url="https://example.com/a",
        body="Đà Nẵng tổ chức lễ hội pháo hoa quốc tế vào mùa hè năm nay với nhiều đội thi tham gia.",
        confidence=90))
    hub.review_draft(first["id"], hub.DraftReview(review_status="approved"))
    second = hub.create_draft(hub.DraftCreate(
        page_id="p1", title="Lễ hội pháo hoa quốc tế tổ chức tại Đà Nẵng",
        source_url="https://example.com/b",
        body="Lễ hội pháo hoa quốc tế tại Đà Nẵng diễn ra mùa hè năm nay, có nhiều đội thi tham dự.",
        confidence=95))
    assert second["review_status"] == "held"


def test_same_story_is_allowed_on_a_different_page(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    with hub.connect() as conn:
        stamp = hub.now()
        conn.execute("INSERT INTO fb_pages(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                     ("p2", "Page khác", stamp, stamp))
    first = hub.create_draft(hub.DraftCreate(
        page_id="p1", title="Lịch bắn pháo hoa Đà Nẵng 2026",
        source_url="https://example.com/events/fireworks",
        body="Lịch bắn pháo hoa Đà Nẵng năm 2026 được công bố.",
        confidence=90))
    hub.review_draft(first["id"], hub.DraftReview(review_status="approved"))
    second = hub.create_draft(hub.DraftCreate(
        page_id="p2", title="Lịch bắn pháo hoa Đà Nẵng 2026",
        source_url="https://example.com/events/fireworks",
        body="Lịch bắn pháo hoa Đà Nẵng năm 2026 được công bố.",
        confidence=90))
    assert second["review_status"] == "pending"


def test_same_story_with_material_new_information_is_allowed_as_update(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    url = "https://example.com/events/fireworks"
    first = hub.create_draft(hub.DraftCreate(
        page_id="p1", title="Lịch bắn pháo hoa Đà Nẵng 2026",
        source_url=url,
        body="Lễ hội pháo hoa quốc tế Đà Nẵng diễn ra vào mùa hè năm 2026 với nhiều đội thi.",
        confidence=90))
    hub.review_draft(first["id"], hub.DraftReview(review_status="approved"))
    updated = hub.create_draft(hub.DraftCreate(
        page_id="p1", title="Cập nhật lịch pháo hoa Đà Nẵng 2026",
        source_url=url,
        body=("Ban tổ chức vừa công bố thêm thông tin mới: đêm khai mạc bắt đầu lúc 20 giờ "
              "ngày 30 tháng 5, đội tuyển Nhật Bản và Canada tham gia, khu vực khán đài "
              "ven sông Hàn mở bán vé trực tuyến từ tuần tới."),
        confidence=90))
    assert updated["review_status"] == "pending"
    assert "trùng nội dung" not in updated["review_reason"].lower()


def test_same_url_without_material_new_information_stays_held(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    url = "https://example.com/events/fireworks"
    first = hub.create_draft(hub.DraftCreate(
        page_id="p1", title="Lịch bắn pháo hoa Đà Nẵng 2026",
        source_url=url,
        body="Lễ hội pháo hoa quốc tế Đà Nẵng diễn ra vào mùa hè năm 2026 với nhiều đội thi.",
        confidence=90))
    hub.review_draft(first["id"], hub.DraftReview(review_status="approved"))
    repeat = hub.create_draft(hub.DraftCreate(
        page_id="p1", title="Lịch pháo hoa Đà Nẵng năm 2026",
        source_url=url,
        body="Lễ hội pháo hoa quốc tế tại Đà Nẵng diễn ra mùa hè năm 2026 với nhiều đội thi.",
        confidence=90))
    assert repeat["review_status"] == "held"



def test_distinct_hosts_without_declared_publisher_groups_do_not_count_as_independent(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    add_item(path, "i1", "s1", "Đà Nẵng tổ chức lễ hội pháo hoa", "https://news-a.example/a")
    add_item(path, "i2", "s2", "Lễ hội pháo hoa Đà Nẵng tổ chức", "https://news-b.example/b")
    add_item(path, "i3", "s3", "Đà Nẵng tổ chức lễ hội pháo hoa", "https://news-c.example/c")
    with hub.connect() as conn:
        for sid in ("s1", "s2", "s3"):
            conn.execute("UPDATE fb_sources SET trust_level='established' WHERE id=?", (sid,))
    result = cp.process_item("i1")
    assert result["results"][0]["status"] == "held"
    with hub.connect() as conn:
        draft = conn.execute("SELECT * FROM fb_drafts").fetchone()
        assert draft["review_status"] == "held"
        assert "không được tính" in draft["review_reason"].lower()


def test_multiple_hosts_in_one_publisher_group_count_as_one_trusted_group(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    add_item(path, "i1", "s1", "Đà Nẵng tổ chức lễ hội pháo hoa", "https://www.publisher.example/a")
    add_item(path, "i2", "s2", "Lễ hội pháo hoa Đà Nẵng tổ chức", "https://news.publisher.example/b")
    add_item(path, "i3", "s3", "Đà Nẵng tổ chức lễ hội pháo hoa", "https://media.publisher.example/c")
    with hub.connect() as conn:
        for sid in ("s1", "s2", "s3"):
            conn.execute(
                "UPDATE fb_sources SET trust_level='established', publisher_group='same-publisher' WHERE id=?",
                (sid,),
            )
    result = cp.process_item("i1")
    assert result["results"][0]["status"] == "held"
    with hub.connect() as conn:
        draft = conn.execute("SELECT * FROM fb_drafts").fetchone()
        assert draft["review_status"] == "held"
        assert "1 nhóm nhà xuất bản độc lập" in draft["review_reason"].lower()
