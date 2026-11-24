from __future__ import annotations

from app import content_processor as cp
from app import evidence_checker as ec
from app import facebook_hub as hub


def seed(path):
    hub.DB_PATH = path
    hub.init_hub_db()
    with hub.connect() as conn:
        stamp = hub.now()
        conn.execute("INSERT INTO fb_pages(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                     ("p1", "Page Tin tức", stamp, stamp))
        for sid, name in [
            ("s1", "Nguồn A"), ("s2", "Nguồn B"), ("s3", "Nguồn C"),
        ]:
            conn.execute("INSERT INTO fb_sources(id,name,url,source_type,created_at) VALUES(?,?,?,?,?)",
                         (sid, name, f"https://{sid}.example/rss", "rss", stamp))
            conn.execute("INSERT INTO fb_page_sources(page_id,source_id,created_at) VALUES(?,?,?)",
                         ("p1", sid, stamp))


def add_item(item_id, source_id, title, summary):
    with hub.connect() as conn:
        conn.execute("""INSERT INTO fb_source_items
          (id,source_id,item_key,title,item_url,summary,first_seen_at)
          VALUES(?,?,?,?,?,?,?)""",
          (item_id, source_id, item_id, title, f"https://{source_id}.example/{item_id}",
           summary, hub.now()))


def test_extracts_only_explicit_dates_times_and_metrics():
    claims = ec.extract_claims(
        "Sự kiện diễn ra ngày 30/05/2026 lúc 20 giờ",
        "Dự kiến có 12.000 người tham dự, ngân sách 5 tỷ đồng.",
    )
    values = {(kind, normalized) for kind, _, normalized in claims}
    assert ("date", "30/05/2026") in values
    assert ("time", "20 giờ") in values
    assert ("metric:người", "12.000 người") in values
    assert ("metric:tỷ", "5 tỷ") in values


def test_same_claim_supported_by_two_declared_trusted_groups(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    add_item("i1", "s1", "Lễ hội Đà Nẵng ngày 30/05/2026", "Khai mạc lúc 20 giờ.")
    add_item("i2", "s2", "Thông tin lễ hội Đà Nẵng ngày 30/05/2026", "Chương trình bắt đầu lúc 20 giờ.")
    with hub.connect() as conn:
        conn.execute("UPDATE fb_sources SET trust_level='established', publisher_group='publisher-a' WHERE id='s1'")
        conn.execute("UPDATE fb_sources SET trust_level='established', publisher_group='publisher-b' WHERE id='s2'")
    cp.process_item("i1")
    cp.process_item("i2")
    with hub.connect() as conn:
        draft_id = conn.execute("SELECT id FROM fb_drafts").fetchone()["id"]
    result = ec.check_draft(draft_id)
    assert result["status"] == "supported"
    assert result["counts"]["supported"] >= 1
    with hub.connect() as conn:
        claim = conn.execute("SELECT * FROM fb_evidence_claims WHERE draft_id=? AND claim_type='date'",
                             (draft_id,)).fetchone()
        assert claim["status"] == "supported"
        assert conn.execute("SELECT COUNT(*) FROM fb_evidence_records WHERE claim_id=?",
                            (claim["id"],)).fetchone()[0] == 2
        draft = conn.execute("SELECT review_status FROM fb_drafts WHERE id=?", (draft_id,)).fetchone()
        assert draft["review_status"] == "held"
    dashboard = hub.get_draft_evidence(draft_id)
    assert dashboard["publication_allowed"] is False
    assert dashboard["claim_count"] >= 1
    date_claim = next(item for item in dashboard["claims"] if item["claim_type"] == "date")
    assert len(date_claim["evidence"]) == 2
    assert all(item["source_url"].startswith("https://") for item in date_claim["evidence"])


def test_single_source_claim_is_insufficient_and_draft_stays_held(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    add_item("i1", "s1", "Sự kiện diễn ra ngày 30/05/2026", "Bắt đầu lúc 20 giờ.")
    cp.process_item("i1")
    with hub.connect() as conn:
        draft_id = conn.execute("SELECT id FROM fb_drafts").fetchone()["id"]
    result = ec.check_draft(draft_id)
    assert result["status"] == "insufficient"
    assert result["counts"]["insufficient"] >= 1
    with hub.connect() as conn:
        assert conn.execute("SELECT review_status FROM fb_drafts WHERE id=?", (draft_id,)).fetchone()[0] == "held"


def test_disagreement_is_only_contradicted_when_both_values_have_independent_support(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    add_item("i1", "s1", "Sự kiện có 12 người tham dự", "Thông tin từ ban tổ chức.")
    add_item("i2", "s2", "Sự kiện có 12 người tham dự", "Số người dự kiến là 12.")
    add_item("i3", "s3", "Sự kiện có 15 người tham dự", "Thông tin từ đơn vị khác.")
    with hub.connect() as conn:
        for sid, group in [("s1", "publisher-a"), ("s2", "publisher-b"), ("s3", "publisher-c")]:
            conn.execute("UPDATE fb_sources SET trust_level='established', publisher_group=? WHERE id=?",
                         (group, sid))
    cp.process_item("i1")
    cp.process_item("i2")
    cp.process_item("i3")
    with hub.connect() as conn:
        draft_id = conn.execute("SELECT id FROM fb_drafts").fetchone()["id"]
    result = ec.check_draft(draft_id)
    # One isolated disagreement is not strong enough to label a fact contradicted.
    assert result["counts"]["contradicted"] == 0
    assert result["status"] == "insufficient"



def test_ai_added_date_and_metric_are_flagged_unsupported(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    seed(path)
    add_item("i1", "s1", "Sự kiện tại Đà Nẵng", "Ban tổ chức đang chuẩn bị chương trình.")
    cp.process_item("i1")
    with hub.connect() as conn:
        draft_id = conn.execute("SELECT id FROM fb_drafts").fetchone()["id"]
        conn.execute(
            "UPDATE fb_drafts SET title=?, body=? WHERE id=?",
            ("Thông báo sự kiện", "Sự kiện diễn ra ngày 31/12/2026 với 99 người tham dự.", draft_id),
        )
    result = ec.check_draft(draft_id)
    assert result["status"] == "unsupported"
    assert result["counts"]["unsupported"] >= 2
    with hub.connect() as conn:
        claims = conn.execute(
            "SELECT claim_type, normalized_value, status, source_count "
            "FROM fb_evidence_claims WHERE draft_id=? AND status='unsupported'",
            (draft_id,),
        ).fetchall()
        assert len(claims) >= 2
        assert all(row["source_count"] == 0 for row in claims)
        assert conn.execute(
            "SELECT review_status FROM fb_drafts WHERE id=?", (draft_id,)
        ).fetchone()[0] == "held"
    # Re-checking must refresh rather than duplicate the ledger.
    again = ec.check_draft(draft_id)
    assert again["counts"]["unsupported"] >= 2
