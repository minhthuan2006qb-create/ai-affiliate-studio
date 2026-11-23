from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app import facebook_hub as hub
from app import facebook_scheduler as scheduler


def seed_page(path, page_id="p1"):
    hub.DB_PATH = path
    hub.init_hub_db()
    scheduler.init_scheduler_db()
    stamp = hub.now()
    with hub.connect() as conn:
        conn.execute("INSERT INTO fb_pages(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                     (page_id, "Page lịch", stamp, stamp))


def add_draft(page_id, status, title):
    with hub.connect() as conn:
        draft_id = title.lower().replace(" ", "-")
        stamp = hub.now()
        conn.execute("""
          INSERT INTO fb_drafts
          (id,page_id,title,body,confidence,review_status,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?)
        """, (draft_id, page_id, title, "Nội dung bài viết đủ dài để kiểm tra lịch đăng.", 90,
              status, stamp, stamp))
    return draft_id


def configure(page_id="p1", **overrides):
    values = {
        "enabled": True, "timezone": "Asia/Ho_Chi_Minh",
        "posting_times": ["09:00", "10:00", "11:00"],
        "max_posts_per_day": 2, "min_interval_minutes": 90,
    }
    values.update(overrides)
    scheduler.write_settings(page_id, scheduler.ScheduleSettings(**values))


def test_scheduler_only_queues_approved_and_respects_daily_limit_and_spacing(tmp_path):
    path = tmp_path / "scheduler.sqlite3"
    seed_page(path)
    add_draft("p1", "approved", "Bài đã duyệt một")
    add_draft("p1", "approved", "Bài đã duyệt hai")
    add_draft("p1", "approved", "Bài đã duyệt ba")
    add_draft("p1", "pending", "Bài đang chờ")
    add_draft("p1", "held", "Bài bị giữ")
    configure()
    result = scheduler.build_schedule("p1", scheduler.PlanRequest(days=7))
    assert result["count"] == 3
    with hub.connect() as conn:
        rows = conn.execute("""
          SELECT q.planned_at, d.review_status FROM fb_publish_schedule q
          JOIN fb_drafts d ON d.id=q.draft_id ORDER BY q.planned_at
        """).fetchall()
    assert all(row["review_status"] == "approved" for row in rows)
    zone = ZoneInfo("Asia/Ho_Chi_Minh")
    planned = [datetime.fromisoformat(row["planned_at"]).astimezone(zone) for row in rows]
    per_day = {}
    for item in planned:
        per_day[item.date()] = per_day.get(item.date(), 0) + 1
    assert max(per_day.values()) <= 2
    assert all((planned[i + 1] - planned[i]).total_seconds() >= 90 * 60
               for i in range(len(planned) - 1))


def test_paused_page_cannot_generate_schedule(tmp_path):
    path = tmp_path / "scheduler.sqlite3"
    seed_page(path)
    add_draft("p1", "approved", "Bài đã duyệt")
    configure(enabled=False)
    with pytest.raises(HTTPException) as exc:
        scheduler.build_schedule("p1", scheduler.PlanRequest(days=7))
    assert exc.value.status_code == 409


def test_settings_are_isolated_per_page(tmp_path):
    path = tmp_path / "scheduler.sqlite3"
    seed_page(path, "p1")
    stamp = hub.now()
    with hub.connect() as conn:
        conn.execute("INSERT INTO fb_pages(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                     ("p2", "Page thứ hai", stamp, stamp))
    configure("p1", max_posts_per_day=5, posting_times=["08:00", "12:00"])
    configure("p2", max_posts_per_day=1, posting_times=["20:00"])
    first = scheduler.read_settings("p1")
    second = scheduler.read_settings("p2")
    assert first["max_posts_per_day"] == 5
    assert second["max_posts_per_day"] == 1
    assert first["posting_times"] != second["posting_times"]


def test_invalid_timezone_and_duplicate_times_are_rejected():
    with pytest.raises(ValueError):
        scheduler.ScheduleSettings(timezone="Mars/Olympus")
    with pytest.raises(ValueError):
        scheduler.ScheduleSettings(posting_times=["09:00", "09:00"])



def test_revoking_approval_cancels_queued_schedule(tmp_path):
    path = tmp_path / "scheduler.sqlite3"
    seed_page(path)
    draft_id = add_draft("p1", "approved", "Bài cần thu hồi duyệt")
    configure()
    result = scheduler.build_schedule("p1", scheduler.PlanRequest(days=7))
    assert result["count"] == 1
    hub.review_draft(draft_id, hub.DraftReview(review_status="held", review_reason="Cần xác minh lại"))
    with hub.connect() as conn:
        row = conn.execute("SELECT state FROM fb_publish_schedule WHERE draft_id=?",
                           (draft_id,)).fetchone()
    assert row["state"] == "cancelled"


def test_page_pause_and_resume_suspends_only_approved_queue_items(tmp_path):
    path = tmp_path / "scheduler.sqlite3"
    seed_page(path)
    draft_id = add_draft("p1", "approved", "Bài tạm dừng theo Page")
    configure()
    scheduler.build_schedule("p1", scheduler.PlanRequest(days=7))
    configure(enabled=False)
    with hub.connect() as conn:
        row = conn.execute("SELECT state FROM fb_publish_schedule WHERE draft_id=?",
                           (draft_id,)).fetchone()
    assert row["state"] == "paused"
    configure(enabled=True)
    with hub.connect() as conn:
        row = conn.execute("SELECT state FROM fb_publish_schedule WHERE draft_id=?",
                           (draft_id,)).fetchone()
    assert row["state"] == "queued"
