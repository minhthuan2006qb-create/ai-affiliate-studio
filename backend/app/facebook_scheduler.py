from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .facebook_hub import connect, log, now, require_hub_key

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
              WHERE page_id=? AND state='queued'
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


@router.post("/pages/{page_id}/schedule/plan")
def build_schedule(page_id: str, payload: PlanRequest):
    init_scheduler_db()
    stamp = datetime.now(timezone.utc)
    created = []
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_pages WHERE id=?", (page_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy Page.")
        settings = get_settings(conn, page_id)
        if not settings["enabled"]:
            raise HTTPException(409, "Lịch của Page đang tạm dừng. Hãy bật lịch trước.")
        zone = ZoneInfo(settings["timezone"])
        drafts = conn.execute("""
          SELECT d.* FROM fb_drafts d
          WHERE d.page_id=? AND d.review_status='approved'
            AND NOT EXISTS (SELECT 1 FROM fb_publish_schedule q WHERE q.draft_id=d.id)
          ORDER BY d.confidence DESC, d.created_at ASC
        """, (page_id,)).fetchall()
        existing = conn.execute("""
          SELECT planned_at FROM fb_publish_schedule
          WHERE page_id=? AND state='queued' ORDER BY planned_at
        """, (page_id,)).fetchall()
        occupied = [datetime.fromisoformat(row["planned_at"]).astimezone(zone) for row in existing]
        for draft in drafts:
            chosen = None
            for offset in range(payload.days):
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
              INSERT INTO fb_publish_schedule(id,page_id,draft_id,planned_at,state,created_at,updated_at)
              VALUES(?,?,?,?,'queued',?,?)
            """, (queue_id, page_id, draft["id"], planned, now(), now()))
            occupied.append(chosen)
            created.append({"id": queue_id, "draft_id": draft["id"], "title": draft["title"],
                            "planned_at": planned, "state": "queued"})
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
          WHERE id=? AND state='queued'
        """, (state, now(), schedule_id))
        if not cur.rowcount:
            raise HTTPException(404, "Không tìm thấy lịch đang chờ.")
        log(conn, "schedule.cancelled", schedule_id)
    return {"ok": True, "state": state}
