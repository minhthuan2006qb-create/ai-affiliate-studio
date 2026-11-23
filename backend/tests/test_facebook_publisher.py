from __future__ import annotations

from app import facebook_hub as hub
from app import facebook_scheduler as scheduler
from app import facebook_publisher as publisher


def _seed(path, auto_publish=False):
    hub.DB_PATH = path
    hub.init_hub_db()
    scheduler.init_scheduler_db()
    publisher.init_publisher_db()
    stamp = hub.now()
    with hub.connect() as conn:
        conn.execute("""
          INSERT INTO fb_pages(id,name,meta_page_id,auto_publish,created_at,updated_at)
          VALUES('p1','Page test','meta-p1',?,?,?)
        """, (int(auto_publish), stamp, stamp))
        conn.execute("""
          INSERT INTO fb_drafts
          (id,page_id,title,body,confidence,review_status,created_at,updated_at)
          VALUES('d1','p1','Tiêu đề kiểm thử','Nội dung kiểm thử đủ dài và đã được người quản trị duyệt.',95,
                 'approved',?,?)
        """, (stamp, stamp))
    scheduler.write_settings("p1", scheduler.ScheduleSettings(
        enabled=True, timezone="Asia/Ho_Chi_Minh", posting_times=["09:00"],
        max_posts_per_day=2, min_interval_minutes=60))
    planned = scheduler.build_schedule("p1", scheduler.PlanRequest(days=7))
    schedule_id = planned["created"][0]["id"]
    with hub.connect() as conn:
        conn.execute("UPDATE fb_publish_schedule SET planned_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                     (schedule_id,))
    result = scheduler.process_due_schedules()
    assert result["ready"] == 1
    return schedule_id


def test_global_publish_switch_off_does_not_change_ready_queue(tmp_path, monkeypatch):
    schedule_id = _seed(tmp_path / "publisher.sqlite3", auto_publish=True)
    monkeypatch.setattr(publisher, "PUBLISH_ENABLED", False)
    result = publisher.process_ready_items()
    assert result["disabled"] is True
    with hub.connect() as conn:
        state = conn.execute("SELECT state FROM fb_publish_schedule WHERE id=?",
                             (schedule_id,)).fetchone()["state"]
    assert state == "ready"


def test_page_auto_publish_off_keeps_ready_item_for_later_resume(tmp_path, monkeypatch):
    schedule_id = _seed(tmp_path / "publisher.sqlite3", auto_publish=False)
    monkeypatch.setattr(publisher, "PUBLISH_ENABLED", True)
    assert publisher._claim_due_item(schedule_id) is None
    with hub.connect() as conn:
        row = conn.execute("SELECT state FROM fb_publish_schedule WHERE id=?",
                           (schedule_id,)).fetchone()
    assert row["state"] == "ready"


def test_interrupted_publish_is_marked_unknown_not_retried(tmp_path, monkeypatch):
    schedule_id = _seed(tmp_path / "publisher.sqlite3", auto_publish=True)
    monkeypatch.setattr(publisher, "PUBLISH_ENABLED", True)
    with hub.connect() as conn:
        conn.execute("""
          UPDATE fb_publish_schedule SET state='publishing',publish_attempt_id='attempt-1',
            publish_attempted_at='2000-01-01T00:00:00+00:00'
          WHERE id=?
        """, (schedule_id,))
    result = publisher.process_ready_items()
    assert result["interrupted_marked_unknown"] == 1
    assert result["processed"] == 0
    with hub.connect() as conn:
        row = conn.execute("SELECT state,publish_error FROM fb_publish_schedule WHERE id=?",
                           (schedule_id,)).fetchone()
    assert row["state"] == "unknown"
    assert "đối soát" in row["publish_error"]


def test_final_gate_blocks_if_approval_is_revoked_after_claim(tmp_path, monkeypatch):
    schedule_id = _seed(tmp_path / "publisher.sqlite3", auto_publish=True)
    monkeypatch.setattr(publisher, "PUBLISH_ENABLED", True)
    stamp = hub.now()
    with hub.connect() as conn:
        conn.execute("""
          INSERT INTO fb_meta_page_tokens
          (page_id,meta_page_id,token_ciphertext,granted_scopes,connected_at,updated_at)
          VALUES('p1','meta-p1','encrypted-placeholder','pages_manage_posts,pages_read_engagement,pages_show_list',?,?)
        """, (stamp, stamp))
    item = publisher._claim_due_item(schedule_id)
    assert item is not None
    with hub.connect() as conn:
        conn.execute("UPDATE fb_drafts SET review_status='held' WHERE id='d1'")
    assert publisher._still_eligible_to_send(item) is False
    with hub.connect() as conn:
        state = conn.execute("SELECT state FROM fb_publish_schedule WHERE id=?",
                             (schedule_id,)).fetchone()["state"]
    assert state == "blocked"
