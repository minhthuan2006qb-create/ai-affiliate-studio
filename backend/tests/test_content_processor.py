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
