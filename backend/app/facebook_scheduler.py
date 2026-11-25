from __future__ import annotations

import argparse
import hashlib
import json
import time as time_module
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .facebook_hub import connect, log, now, require_hub_key
from .duplicate_guard import find_duplicate

router = APIRouter(prefix="/api/facebook-hub", tags=["Facebook Hub Scheduler"],
                   dependencies=[Depends(require_hub_key)])

DEFAULT_TIMES = ["09:00", "13:00", "19:00"]


def init_scheduler_db() -> None:
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS fb_page_schedule_settings (
          page_id TEXT PRIMARY KEY REFERENCES fb_pages(id) ON DELETE CASCADE,
          enabled INTEGER NOT NULL DEFAULT 0,
          timezone TEXT NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
          posting_times TEXT NOT NULL DEFAULT '[ "09:00", "13:00", "19:00" ]',
          max_posts_per_day INTEGER NOT NULL DEFAULT 3,
          min_interval_minutes INTEGER NOT NULL DEFAULT 180,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fb_publish_schedule (
          id TEXT PRIMARY KEY,
          page_id TEXT NOT NULL REFERENCES fb_pages(id) ON DELETE CASCADE,
          draft_id TEXT NOT NULL UNIQUE REFERENCES fb_drafts(id) ON DELETE CASCADE,
          planned_at TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'queued',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fb_schedule_page_time
          ON fb_publish_schedule(page_id, planned_at);
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(fb_publish_schedule)")}
        if "content_hash" not in columns:
            conn.execute("ALTER TABLE fb_publish_schedule ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")


def draft_content_hash(draft) -> str:
    """Stable snapshot of the exact content approved and queued for this slot."""
    payload = {
        "title": draft["title"] or "",
        "body": draft["body"] or "",
        "source_url": draft["source_url"] or "",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@router.on_event("startup")
def startup_scheduler() -> None:
    init_scheduler_db()


class ScheduleSettings(BaseModel):
    enabled: bool = False
    timezone: str = Field(default="Asia/Ho_Chi_Minh", min_length=1, max_length=80)
    posting_times: list[str] = Field(default_factory=lambda: DEFAULT_TIMES.copy(), min_length=1, max_length=12)
    max_posts_per_day: int = Field(default=3, ge=1, le=12)
    min_interval_minutes: int = Field(default=180, ge=15, le=1440)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Múi giờ không hợp lệ.")
        return value

    @field_validator("posting_times")
    @classmethod
    def valid_times(cls, values: list[str]) -> list[str]:
        parsed = []
        for value in values:
            try:
                datetime.strptime(value, "%H:%M")
            except ValueError:
                raise ValueError("Giờ đăng phải theo định dạng HH:MM.")
            if value in parsed:
                raise ValueError("Không được lặp giờ đăng.")
            parsed.append(value)
        return sorted(parsed)


class PlanRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=30)


def get_settings(conn, page_id: str) -> dict:
    row = conn.execute("SELECT * FROM fb_page_schedule_settings WHERE page_id=?", (page_id,)).fetchone()
    if row is None:
        return {"page_id": page_id, "enabled": False, "timezone": "Asia/Ho_Chi_Minh",
                "posting_times": DEFAULT_TIMES.copy(), "max_posts_per_day": 3,
                "min_interval_minutes": 180}
    result = dict(row)
    result["enabled"] = bool(result["enabled"])
    result["posting_times"] = json.loads(result["posting_times"])
    return result


@router.get("/pages/{page_id}/schedule-settings")
def read_settings(page_id: str):
    init_scheduler_db()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_pages WHERE id=?", (page_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy Page.")
        return get_settings(conn, page_id)


@router.put("/pages/{page_id}/schedule-settings")
def write_settings(page_id: str, payload: ScheduleSettings):
    init_scheduler_db()
    values = payload.model_dump()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_pages WHERE id=?", (page_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy Page.")
        conn.execute("""
          INSERT INTO fb_page_schedule_settings
          (page_id,enabled,timezone,posting_times,max_posts_per_day,min_interval_minutes,updated_at)
          VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(page_id) DO UPDATE SET enabled=excluded.enabled, timezone=excluded.timezone,
          posting_times=excluded.posting_times, max_posts_per_day=excluded.max_posts_per_day,
          min_interval_minutes=excluded.min_interval_minutes, updated_at=excluded.updated_at
        """, (page_id, int(values["enabled"]), values["timezone"],
              json.dumps(values["posting_times"]), values["max_posts_per_day"],
              values["min_interval_minutes"], now()))
        # Pausing a Page suspends queued slots; resuming restores only
        # slots whose drafts are still approved.
        if not values["enabled"]:
            conn.execute("""
              UPDATE fb_publish_schedule SET state='paused', updated_at=?
              WHERE page_id=? AND state IN ('queued','ready')
            """, (now(), page_id))
        else:
            conn.execute("""
              UPDATE fb_publish_schedule SET state='queued', updated_at=?
              WHERE page_id=? AND state='paused'
                AND EXISTS (SELECT 1 FROM fb_drafts d
                            WHERE d.id=fb_publish_schedule.draft_id
                              AND d.review_status='approved')
            """, (now(), page_id))
            conn.execute("""
              UPDATE fb_publish_schedule SET state='cancelled', updated_at=?
              WHERE page_id=? AND state='paused'
                AND NOT EXISTS (SELECT 1 FROM fb_drafts d
                                WHERE d.id=fb_publish_schedule.draft_id
                                  AND d.review_status='approved')
            """, (now(), page_id))
        log(conn, "schedule.settings_updated", page_id)
    return {"ok": True, "page_id": page_id}


def plan_page(conn, page_id: str, days: int, newest_first: bool = False,
              only_draft_ids: set[str] | None = None) -> list[dict]:
    """Place approved, unscheduled drafts into the Page's free posting slots."""
    stamp = datetime.now(timezone.utc)
    created: list[dict] = []
    settings = get_settings(conn, page_id)
    zone = ZoneInfo(settings["timezone"])
    order = "d.created_at DESC" if newest_first else "d.confidence DESC, d.created_at ASC"
    drafts = conn.execute(f"""
      SELECT d.* FROM fb_drafts d
      WHERE d.page_id=? AND d.review_status='approved'
        AND NOT EXISTS (SELECT 1 FROM fb_publish_schedule q WHERE q.draft_id=d.id)
      ORDER BY {order}
    """, (page_id,)).fetchall()
    if only_draft_ids is not None:
        drafts = [d for d in drafts if d["id"] in only_draft_ids]
    existing = conn.execute("""
      SELECT planned_at FROM fb_publish_schedule
      WHERE page_id=? AND state IN ('queued','ready','publishing','published') ORDER BY planned_at
    """, (page_id,)).fetchall()
    occupied = [datetime.fromisoformat(row["planned_at"]).astimezone(zone) for row in existing]
    for draft in drafts:
        chosen = None
        for offset in range(days):
            day = (stamp.astimezone(zone).date() + timedelta(days=offset))
            day_count = sum(1 for item in occupied if item.date() == day)
            if day_count >= settings["max_posts_per_day"]:
                continue
            for clock in settings["posting_times"]:
                hour, minute = map(int, clock.split(":"))
                candidate = datetime.combine(day, time(hour, minute), tzinfo=zone)
                if candidate <= stamp.astimezone(zone):
                    continue
                if any(abs((candidate - item).total_seconds()) < settings["min_interval_minutes"] * 60
                       for item in occupied):
                    continue
                chosen = candidate
                break
            if chosen:
                break
        if chosen is None:
            break
        queue_id = str(uuid.uuid4())
        planned = chosen.astimezone(timezone.utc).isoformat()
        conn.execute("""
          INSERT INTO fb_publish_schedule
            (id,page_id,draft_id,planned_at,state,created_at,updated_at,content_hash)
          VALUES(?,?,?,?,'queued',?,?,?)
        """, (queue_id, page_id, draft["id"], planned, now(), now(),
              draft_content_hash(draft)))
        occupied.append(chosen)
        created.append({"id": queue_id, "draft_id": draft["id"], "title": draft["title"],
                        "planned_at": planned, "state": "queued"})
    return created


@router.post("/pages/{page_id}/schedule/plan")
def build_schedule(page_id: str, payload: PlanRequest):
    init_scheduler_db()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_pages WHERE id=?", (page_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy Page.")
        settings = get_settings(conn, page_id)
        if not settings["enabled"]:
            raise HTTPException(409, "Lịch của Page đang tạm dừng. Hãy bật lịch trước.")
        created = plan_page(conn, page_id, payload.days)
        log(conn, "schedule.planned", f"{page_id}: {len(created)} item(s)")
    return {"created": created, "count": len(created),
            "note": "Đây là lịch nội bộ; chưa gửi bài lên Facebook."}


@router.get("/pages/{page_id}/schedule")
def list_schedule(page_id: str):
    init_scheduler_db()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_pages WHERE id=?", (page_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy Page.")
        rows = conn.execute("""
          SELECT q.*, d.title, d.confidence, d.review_status
          FROM fb_publish_schedule q JOIN fb_drafts d ON d.id=q.draft_id
          WHERE q.page_id=? ORDER BY q.planned_at LIMIT 300
        """, (page_id,)).fetchall()
    return {"items": [dict(row) for row in rows]}


@router.patch("/schedule/{schedule_id}")
def update_schedule_item(schedule_id: str, state: Literal["cancelled"]):
    init_scheduler_db()
    with connect() as conn:
        cur = conn.execute("""
          UPDATE fb_publish_schedule SET state=?, updated_at=?
          WHERE id=? AND state IN ('queued','ready')
        """, (state, now(), schedule_id))
        if not cur.rowcount:
            raise HTTPException(404, "Không tìm thấy lịch đang chờ.")
        log(conn, "schedule.cancelled", schedule_id)
    return {"ok": True, "state": state}


@router.post("/schedule/{schedule_id}/validate")
def validate_schedule_item(schedule_id: str):
    """Safety gate only: checks eligibility and never publishes to Facebook."""
    init_scheduler_db()
    with connect() as conn:
        row = conn.execute("""
          SELECT q.*, d.title, d.body, d.source_url, d.review_status, d.page_id AS draft_page_id,
                 p.enabled AS page_enabled, COALESCE(ss.enabled, 0) AS schedule_enabled
          FROM fb_publish_schedule q
          JOIN fb_drafts d ON d.id=q.draft_id
          JOIN fb_pages p ON p.id=q.page_id
          LEFT JOIN fb_page_schedule_settings ss ON ss.page_id=q.page_id
          WHERE q.id=?
        """, (schedule_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Không tìm thấy lịch đăng.")
        if row["state"] not in {"queued", "ready"}:
            return {"eligible": False, "state": row["state"], "reason": "Lịch không còn ở trạng thái có thể kiểm tra."}
        planned = datetime.fromisoformat(row["planned_at"])
        if planned > datetime.now(timezone.utc):
            return {"eligible": False, "state": "queued", "reason": "Chưa đến giờ dự kiến."}
        reason = None
        if not row["page_enabled"]:
            reason = "Page đang bị tắt."
        elif not row["schedule_enabled"]:
            reason = "Lịch tự động của Page đang tạm dừng."
        elif row["review_status"] != "approved":
            reason = "Bản nháp không còn được duyệt."
        elif row["draft_page_id"] != row["page_id"]:
            reason = "Bản nháp không thuộc Page trong lịch."
        elif not row["content_hash"] or draft_content_hash(row) != row["content_hash"]:
            reason = "Nội dung đã thay đổi sau khi xếp lịch; cần kiểm tra và xếp lịch lại."
        else:
            duplicate = find_duplicate(conn, row["page_id"], row["title"], row["body"],
                                       row["source_url"], exclude_draft_id=row["draft_id"])
            if duplicate:
                reason = f"Nội dung có khả năng trùng bài đã duyệt: {duplicate['title']}."
        if reason:
            conn.execute("UPDATE fb_publish_schedule SET state='blocked', updated_at=? WHERE id=?",
                         (now(), schedule_id))
            log(conn, "schedule.validation_blocked", f"{schedule_id}: {reason}", "warning")
            return {"eligible": False, "state": "blocked", "reason": reason}
        log(conn, "schedule.validation_passed", schedule_id)
        return {"eligible": True, "state": row["state"],
                "reason": "Đạt kiểm tra an toàn; chưa có bài nào được gửi lên Facebook."}


def process_due_schedules(limit: int = 50) -> dict:
    """Revalidate due entries and atomically promote unchanged drafts to ready; never publish."""
    init_scheduler_db()
    stamp = datetime.now(timezone.utc)
    with connect() as conn:
        rows = conn.execute("""
          SELECT id FROM fb_publish_schedule
          WHERE state='queued' AND planned_at<=?
          ORDER BY planned_at LIMIT ?
        """, (stamp.isoformat(), max(1, min(int(limit), 200)))).fetchall()
    checked = ready = blocked = 0
    for item in rows:
        result = validate_schedule_item(item["id"])
        checked += 1
        if not result["eligible"]:
            blocked += int(result.get("state") == "blocked")
            continue

        # Re-read the draft and its settings under a write lock. This closes the
        # gap between the first validation and the queued -> ready transition.
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""
              SELECT q.*, d.title, d.body, d.source_url, d.review_status,
                     d.page_id AS draft_page_id, p.enabled AS page_enabled,
                     COALESCE(ss.enabled, 0) AS schedule_enabled
              FROM fb_publish_schedule q
              JOIN fb_drafts d ON d.id=q.draft_id
              JOIN fb_pages p ON p.id=q.page_id
              LEFT JOIN fb_page_schedule_settings ss ON ss.page_id=q.page_id
              WHERE q.id=? AND q.state='queued'
            """, (item["id"],)).fetchone()
            reason = None
            if row is None:
                continue
            if datetime.fromisoformat(row["planned_at"]) > datetime.now(timezone.utc):
                reason = "Chưa đến giờ dự kiến."
            elif not row["page_enabled"]:
                reason = "Page đang bị tắt."
            elif not row["schedule_enabled"]:
                reason = "Lịch tự động của Page đang tạm dừng."
            elif row["review_status"] != "approved":
                reason = "Bản nháp không còn được duyệt."
            elif row["draft_page_id"] != row["page_id"]:
                reason = "Bản nháp không thuộc Page trong lịch."
            elif not row["content_hash"] or draft_content_hash(row) != row["content_hash"]:
                reason = "Nội dung đã thay đổi sau khi xếp lịch; cần kiểm tra và xếp lịch lại."
            else:
                duplicate = find_duplicate(conn, row["page_id"], row["title"], row["body"],
                                           row["source_url"], exclude_draft_id=row["draft_id"])
                if duplicate:
                    reason = f"Nội dung có khả năng trùng bài đã duyệt: {duplicate['title']}."
            if reason:
                conn.execute("""
                  UPDATE fb_publish_schedule SET state='blocked', updated_at=?
                  WHERE id=? AND state='queued'
                """, (now(), item["id"]))
                log(conn, "schedule.validation_blocked", f"{item['id']}: {reason}", "warning")
                blocked += 1
                continue
            cur = conn.execute("""
              UPDATE fb_publish_schedule SET state='ready', updated_at=?
              WHERE id=? AND state='queued' AND content_hash=?
                AND EXISTS (
                  SELECT 1 FROM fb_drafts d
                  JOIN fb_pages p ON p.id=fb_publish_schedule.page_id
                  JOIN fb_page_schedule_settings ss ON ss.page_id=fb_publish_schedule.page_id
                  WHERE d.id=fb_publish_schedule.draft_id
                    AND d.review_status='approved' AND p.enabled=1 AND ss.enabled=1
                    AND d.page_id=fb_publish_schedule.page_id
                )
            """, (now(), item["id"], row["content_hash"]))
            if cur.rowcount:
                ready += 1
                log(conn, "schedule.ready_for_publisher", item["id"])
    return {"checked": checked, "ready": ready, "blocked": blocked,
            "note": "Chỉ kiểm tra và đánh dấu sẵn sàng; không đăng lên Facebook."}


def main() -> None:
    parser = argparse.ArgumentParser(description="Facebook Hub due-schedule safety worker")
    parser.add_argument("--once", action="store_true", help="Process due schedule items once and exit")
    parser.add_argument("--interval-seconds", type=int, default=60,
                        help="Polling interval in seconds (minimum 10)")
    parser.add_argument("--limit", type=int, default=50, help="Maximum due items per cycle (1-200)")
    args = parser.parse_args()
    interval = max(10, args.interval_seconds)
    init_scheduler_db()
    while True:
        result = process_due_schedules(args.limit)
        print(json.dumps(result, ensure_ascii=False))
        if args.once:
            break
        time_module.sleep(interval)


if __name__ == "__main__":
    main()