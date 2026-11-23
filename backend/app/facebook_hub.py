from __future__ import annotations

import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .duplicate_guard import find_duplicate, remember_approved

router = APIRouter(prefix="/api/facebook-hub", tags=["Facebook Hub"])
DB_PATH = Path(os.getenv("FACEBOOK_HUB_DB", os.getenv("DATABASE_PATH", str(Path(__file__).resolve().parents[1] / "data" / "jobs.sqlite3"))))
API_KEY = os.getenv("API_KEY", "")
# Separate key is supported for deployments that want independent access control.
HUB_API_KEY = os.getenv("FACEBOOK_HUB_API_KEY", API_KEY)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_hub_db() -> None:
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS fb_pages (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, meta_page_id TEXT UNIQUE,
          connection_status TEXT NOT NULL DEFAULT 'not_connected',
          enabled INTEGER NOT NULL DEFAULT 1, auto_publish INTEGER NOT NULL DEFAULT 0,
          editorial_tone TEXT NOT NULL DEFAULT 'Trung lập, rõ ràng, dễ hiểu',
          editorial_audience TEXT NOT NULL DEFAULT 'Độc giả phổ thông tại Việt Nam',
          editorial_topics TEXT NOT NULL DEFAULT '',
          editorial_avoid TEXT NOT NULL DEFAULT '',
          editorial_guidance TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fb_sources (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT,
          source_type TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          trust_level TEXT NOT NULL DEFAULT 'unverified',
          publisher_group TEXT NOT NULL DEFAULT '',
          is_official INTEGER NOT NULL DEFAULT 0,
          trust_notes TEXT NOT NULL DEFAULT '',
          last_checked_at TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fb_page_sources (
          page_id TEXT NOT NULL REFERENCES fb_pages(id) ON DELETE CASCADE,
          source_id TEXT NOT NULL REFERENCES fb_sources(id) ON DELETE CASCADE,
          enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
          PRIMARY KEY(page_id, source_id)
        );
        CREATE TABLE IF NOT EXISTS fb_drafts (
          id TEXT PRIMARY KEY, page_id TEXT NOT NULL REFERENCES fb_pages(id) ON DELETE CASCADE,
          source_name TEXT, source_url TEXT, title TEXT NOT NULL, body TEXT NOT NULL,
          confidence INTEGER NOT NULL DEFAULT 0,
          review_status TEXT NOT NULL DEFAULT 'pending',
          review_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fb_activity (
          id TEXT PRIMARY KEY, action TEXT NOT NULL, status TEXT NOT NULL,
          detail TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fb_source_items (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL REFERENCES fb_sources(id) ON DELETE CASCADE,
          item_key TEXT NOT NULL, title TEXT NOT NULL, item_url TEXT NOT NULL,
          summary TEXT, published_at TEXT, first_seen_at TEXT NOT NULL,
          UNIQUE(source_id, item_key)
        );
        CREATE INDEX IF NOT EXISTS idx_fb_source_items_seen ON fb_source_items(first_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_fb_drafts_status ON fb_drafts(review_status, created_at);
        """)
        # Additive migration for existing installations.
        source_columns = {row["name"] for row in conn.execute("PRAGMA table_info(fb_sources)")}
        for column, declaration in {
            "trust_level": "TEXT NOT NULL DEFAULT 'unverified'",
            "publisher_group": "TEXT NOT NULL DEFAULT ''",
            "is_official": "INTEGER NOT NULL DEFAULT 0",
            "trust_notes": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in source_columns:
                conn.execute(f"ALTER TABLE fb_sources ADD COLUMN {column} {declaration}")
        page_columns = {row["name"] for row in conn.execute("PRAGMA table_info(fb_pages)")}
        for column, declaration in {
            "editorial_tone": "TEXT NOT NULL DEFAULT 'Trung lập, rõ ràng, dễ hiểu'",
            "editorial_audience": "TEXT NOT NULL DEFAULT 'Độc giả phổ thông tại Việt Nam'",
            "editorial_topics": "TEXT NOT NULL DEFAULT ''",
            "editorial_avoid": "TEXT NOT NULL DEFAULT ''",
            "editorial_guidance": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in page_columns:
                conn.execute(f"ALTER TABLE fb_pages ADD COLUMN {column} {declaration}")


def require_hub_key(authorization: str | None = Header(default=None)) -> None:
    if not HUB_API_KEY:
        raise HTTPException(503, "Backend chưa cấu hình API_KEY hoặc FACEBOOK_HUB_API_KEY.")
    supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not secrets.compare_digest(supplied, HUB_API_KEY):
        raise HTTPException(401, "API key không hợp lệ.")


class PageCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    meta_page_id: str | None = Field(default=None, max_length=100)


class PageUpdate(BaseModel):
    enabled: bool | None = None
    auto_publish: bool | None = None

class EditorialProfileUpdate(BaseModel):
    editorial_tone: str = Field(default="Trung lập, rõ ràng, dễ hiểu", min_length=2, max_length=160)
    editorial_audience: str = Field(default="Độc giả phổ thông tại Việt Nam", min_length=2, max_length=200)
    editorial_topics: str = Field(default="", max_length=1200)
    editorial_avoid: str = Field(default="", max_length=1200)
    editorial_guidance: str = Field(default="", max_length=1600)


class SourceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    url: str | None = Field(default=None, max_length=2048)
    source_type: Literal["rss", "website", "authorized_api", "manual"]


class SourceTrustUpdate(BaseModel):
    trust_level: Literal["official", "established", "community", "unverified"] = "unverified"
    publisher_group: str = Field(default="", max_length=180)
    is_official: bool = False
    trust_notes: str = Field(default="", max_length=700)


class DraftCreate(BaseModel):
    page_id: str
    source_name: str | None = Field(default=None, max_length=160)
    source_url: str | None = Field(default=None, max_length=2048)
    title: str = Field(min_length=3, max_length=240)
    body: str = Field(min_length=20, max_length=10000)
    confidence: int = Field(default=0, ge=0, le=100)
    review_reason: str | None = Field(default=None, max_length=1000)


class DraftReview(BaseModel):
    review_status: Literal["pending", "approved", "held", "rejected"]
    review_reason: str | None = Field(default=None, max_length=1000)


def log(conn: sqlite3.Connection, action: str, detail: str = "", status: str = "success") -> None:
    conn.execute("INSERT INTO fb_activity VALUES(?,?,?,?,?)",
                 (str(uuid.uuid4()), action, status, detail[:500], now()))


@router.on_event("startup")
def startup_hub() -> None:
    init_hub_db()


@router.get("/dashboard", dependencies=[Depends(require_hub_key)])
def dashboard():
    with connect() as conn:
        counts = {
            "page_count": conn.execute("SELECT COUNT(*) FROM fb_pages").fetchone()[0],
            "source_count": conn.execute("SELECT COUNT(*) FROM fb_sources").fetchone()[0],
            "pending_count": conn.execute("SELECT COUNT(*) FROM fb_drafts WHERE review_status='pending'").fetchone()[0],
            "held_count": conn.execute("SELECT COUNT(*) FROM fb_drafts WHERE review_status='held'").fetchone()[0],
        }
        counts["pages"] = [dict(r) for r in conn.execute("SELECT * FROM fb_pages ORDER BY created_at DESC")]
        counts["sources"] = [dict(r) for r in conn.execute("SELECT * FROM fb_sources ORDER BY created_at DESC")]
        counts["activity"] = [dict(r) for r in conn.execute("SELECT * FROM fb_activity ORDER BY created_at DESC LIMIT 30")]
    for p in counts["pages"]:
        p["enabled"] = bool(p["enabled"]); p["auto_publish"] = bool(p["auto_publish"])
    for s in counts["sources"]:
        s["enabled"] = bool(s["enabled"])
    return counts


@router.post("/pages", dependencies=[Depends(require_hub_key)])
def create_page(payload: PageCreate):
    page_id = str(uuid.uuid4()); stamp = now()
    try:
        with connect() as conn:
            conn.execute("INSERT INTO fb_pages(id,name,meta_page_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                         (page_id, payload.name.strip(), payload.meta_page_id, stamp, stamp))
            log(conn, "page.created", payload.name.strip())
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Meta Page ID này đã được khai báo.")
    return {"id": page_id, "name": payload.name.strip(), "connection_status": "not_connected",
            "enabled": True, "auto_publish": False}


@router.patch("/pages/{page_id}", dependencies=[Depends(require_hub_key)])
def update_page(page_id: str, payload: PageUpdate):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(400, "Không có thiết lập cần cập nhật.")
    with connect() as conn:
        row = conn.execute("SELECT connection_status FROM fb_pages WHERE id=?", (page_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Không tìm thấy Page.")
        # Publishing stays locked until a real Meta OAuth connection is implemented.
        if changes.get("auto_publish") is True and row["connection_status"] != "connected":
            raise HTTPException(409, "Chưa thể bật tự đăng: Page chưa kết nối Meta OAuth.")
        sets = ",".join(f"{k}=?" for k in changes)
        vals = [int(v) if isinstance(v, bool) else v for v in changes.values()]
        vals += [now(), page_id]
        conn.execute(f"UPDATE fb_pages SET {sets}, updated_at=? WHERE id=?", vals)
        log(conn, "page.updated", page_id)
    return {"ok": True}


@router.get("/pages/{page_id}/editorial-profile", dependencies=[Depends(require_hub_key)])
def get_editorial_profile(page_id: str):
    with connect() as conn:
        row = conn.execute("""
          SELECT id, name, editorial_tone, editorial_audience, editorial_topics,
                 editorial_avoid, editorial_guidance
          FROM fb_pages WHERE id=?
        """, (page_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Không tìm thấy Page.")
    return dict(row)


@router.put("/pages/{page_id}/editorial-profile", dependencies=[Depends(require_hub_key)])
def update_editorial_profile(page_id: str, payload: EditorialProfileUpdate):
    values = payload.model_dump()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_pages WHERE id=?", (page_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy Page.")
        conn.execute("""
          UPDATE fb_pages SET editorial_tone=?, editorial_audience=?,
            editorial_topics=?, editorial_avoid=?, editorial_guidance=?, updated_at=?
          WHERE id=?
        """, (values["editorial_tone"].strip(), values["editorial_audience"].strip(),
              values["editorial_topics"].strip(), values["editorial_avoid"].strip(),
              values["editorial_guidance"].strip(), now(), page_id))
        log(conn, "page.editorial_profile_updated", page_id)
    return {"ok": True, "page_id": page_id}


@router.delete("/pages/{page_id}", dependencies=[Depends(require_hub_key)])
def delete_page(page_id: str):
    with connect() as conn:
        cur = conn.execute("DELETE FROM fb_pages WHERE id=?", (page_id,))
        if not cur.rowcount:
            raise HTTPException(404, "Không tìm thấy Page.")
        log(conn, "page.deleted", page_id)
    return {"ok": True}


@router.post("/sources", dependencies=[Depends(require_hub_key)])
def create_source(payload: SourceCreate):
    if payload.source_type != "manual" and not payload.url:
        raise HTTPException(422, "Nguồn RSS/website/API cần có URL.")
    source_id = str(uuid.uuid4())
    with connect() as conn:
        conn.execute("INSERT INTO fb_sources(id,name,url,source_type,created_at) VALUES(?,?,?,?,?)",
                     (source_id, payload.name.strip(), payload.url, payload.source_type, now()))
        log(conn, "source.created", payload.name.strip())
    return {"id": source_id, "name": payload.name.strip(), "url": payload.url,
            "source_type": payload.source_type, "enabled": True}


@router.patch("/sources/{source_id}/trust", dependencies=[Depends(require_hub_key)])
def update_source_trust(source_id: str, payload: SourceTrustUpdate):
    values = payload.model_dump()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_sources WHERE id=?", (source_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy nguồn.")
        conn.execute("""
          UPDATE fb_sources SET trust_level=?, publisher_group=?, is_official=?, trust_notes=?
          WHERE id=?
        """, (values["trust_level"], values["publisher_group"].strip().lower(),
              int(values["is_official"]), values["trust_notes"].strip(), source_id))
        log(conn, "source.trust_updated",
            f"{source_id}: level={values['trust_level']}; group={values['publisher_group']}; official={values['is_official']}")
    return {"ok": True, "source_id": source_id}


@router.delete("/sources/{source_id}", dependencies=[Depends(require_hub_key)])
def delete_source(source_id: str):
    with connect() as conn:
        cur = conn.execute("DELETE FROM fb_sources WHERE id=?", (source_id,))
        if not cur.rowcount:
            raise HTTPException(404, "Không tìm thấy nguồn.")
        log(conn, "source.deleted", source_id)
    return {"ok": True}


@router.post("/pages/{page_id}/sources/{source_id}", dependencies=[Depends(require_hub_key)])
def assign_source(page_id: str, source_id: str):
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_pages WHERE id=?", (page_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy Page.")
        if not conn.execute("SELECT 1 FROM fb_sources WHERE id=?", (source_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy nguồn.")
        conn.execute("INSERT OR IGNORE INTO fb_page_sources VALUES(?,?,1,?)", (page_id, source_id, now()))
        log(conn, "source.assigned", f"{page_id}:{source_id}")
    return {"ok": True}


@router.get("/pages/{page_id}/sources", dependencies=[Depends(require_hub_key)])
def get_page_sources(page_id: str):
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_pages WHERE id=?", (page_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy Page.")
        rows = conn.execute("""SELECT s.*, ps.enabled AS assignment_enabled
          FROM fb_sources s JOIN fb_page_sources ps ON ps.source_id=s.id
          WHERE ps.page_id=? ORDER BY s.name""", (page_id,)).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.delete("/pages/{page_id}/sources/{source_id}", dependencies=[Depends(require_hub_key)])
def unassign_source(page_id: str, source_id: str):
    with connect() as conn:
        conn.execute("DELETE FROM fb_page_sources WHERE page_id=? AND source_id=?", (page_id, source_id))
        log(conn, "source.unassigned", f"{page_id}:{source_id}")
    return {"ok": True}


@router.post("/drafts", dependencies=[Depends(require_hub_key)])
def create_draft(payload: DraftCreate):
    draft_id = str(uuid.uuid4()); stamp = now()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_pages WHERE id=?", (payload.page_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy Page.")
        duplicate = find_duplicate(conn, payload.page_id, payload.title, payload.body, payload.source_url)
        status = "held" if duplicate else ("pending" if payload.confidence >= 70 else "held")
        reason = (f"Trùng nội dung với bài đã duyệt: {duplicate['title']}. {duplicate['reason']}" if duplicate
                  else payload.review_reason or ("Đạt ngưỡng sơ bộ; chờ tích hợp xác minh và chính sách đăng." if status == "pending" else "Độ tin cậy dưới ngưỡng 70; giữ lại để kiểm tra."))
        conn.execute("""INSERT INTO fb_drafts
          (id,page_id,source_name,source_url,title,body,confidence,review_status,review_reason,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
          (draft_id,payload.page_id,payload.source_name,payload.source_url,payload.title,payload.body,
           payload.confidence,status,reason,stamp,stamp))
        log(conn, "draft.created", f"{draft_id}:{status}")
    return {"id": draft_id, "review_status": status, "confidence": payload.confidence, "review_reason": reason}


@router.get("/drafts", dependencies=[Depends(require_hub_key)])
def list_drafts(review_status: str | None = None):
    allowed = {"pending", "approved", "held", "rejected", "published"}
    if review_status and review_status not in allowed:
        raise HTTPException(422, "Trạng thái lọc không hợp lệ.")
    with connect() as conn:
        if review_status:
            rows = conn.execute("""SELECT d.*, p.name AS page_name FROM fb_drafts d
              JOIN fb_pages p ON p.id=d.page_id WHERE d.review_status=?
              ORDER BY d.created_at DESC LIMIT 200""", (review_status,)).fetchall()
        else:
            rows = conn.execute("""SELECT d.*, p.name AS page_name FROM fb_drafts d
              JOIN fb_pages p ON p.id=d.page_id ORDER BY d.created_at DESC LIMIT 200""").fetchall()
    return {"items": [dict(r) for r in rows]}


@router.patch("/drafts/{draft_id}/review", dependencies=[Depends(require_hub_key)])
def review_draft(draft_id: str, payload: DraftReview):
    with connect() as conn:
        draft = conn.execute("SELECT * FROM fb_drafts WHERE id=?", (draft_id,)).fetchone()
        if draft is None:
            raise HTTPException(404, "Không tìm thấy bản nháp.")
        if payload.review_status == "approved":
            duplicate = find_duplicate(conn, draft["page_id"], draft["title"], draft["body"],
                                       draft["source_url"], exclude_draft_id=draft_id)
            if duplicate:
                raise HTTPException(409, f"Bài có khả năng trùng với nội dung đã duyệt: {duplicate['title']}. {duplicate['reason']}")
        conn.execute("UPDATE fb_drafts SET review_status=?, review_reason=?, updated_at=? WHERE id=?",
                     (payload.review_status, payload.review_reason, now(), draft_id))
        if payload.review_status == "approved":
            approved = conn.execute("SELECT * FROM fb_drafts WHERE id=?", (draft_id,)).fetchone()
            remember_approved(conn, approved, now())
        log(conn, "draft.reviewed", f"{draft_id}:{payload.review_status}")
    return {"ok": True, "review_status": payload.review_status}
