"""Short links, click counting and commission ledger for the Facebook Hub.

Policy notes (do not weaken without the project owner's approval):
- A short link is only ever used for a link the reader chooses to click
  (e.g. a "related Shopee products" block on the website). It must never be
  inserted in front of the "Read the original article" link or used to force
  readers through an affiliate redirect.
- Target URLs are limited to an allow-list of Shopee hosts so the redirector
  cannot be abused as an open redirect.
- Raw IP addresses and user agents are not stored; only a salted daily hash is
  kept to estimate unique visitors.
- Commission figures are entered by the operator (manually or from a CSV
  exported from the affiliate dashboard). Nothing here fetches or guesses them.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from .facebook_hub import connect, init_hub_db, log, now, require_hub_key

router = APIRouter(prefix="/api/facebook-hub/affiliate", tags=["Affiliate tracking"],
                   dependencies=[Depends(require_hub_key)])
public_router = APIRouter(tags=["Affiliate short links"])

ALLOWED_TARGET_HOSTS = {"shopee.vn", "s.shopee.vn", "shope.ee", "shp.ee"}
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
CODE_PATTERN = re.compile(r"^[a-z0-9-]{4,40}$")
BOT_UA_PATTERN = re.compile(
    r"bot|crawl|spider|slurp|preview|facebookexternalhit|facebot|whatsapp|"
    r"telegram|zalo|curl|wget|python-requests|headless", re.I)
COMMISSION_STATUSES = ("pending", "approved", "cancelled")


def ensure_affiliate_schema() -> None:
    init_hub_db()
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS fb_affiliate_links (
          id TEXT PRIMARY KEY,
          code TEXT NOT NULL UNIQUE,
          page_id TEXT REFERENCES fb_pages(id) ON DELETE SET NULL,
          draft_id TEXT REFERENCES fb_drafts(id) ON DELETE SET NULL,
          label TEXT NOT NULL,
          target_url TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fb_link_clicks (
          id TEXT PRIMARY KEY,
          link_id TEXT NOT NULL REFERENCES fb_affiliate_links(id) ON DELETE CASCADE,
          clicked_at TEXT NOT NULL,
          click_day TEXT NOT NULL,
          visitor_hash TEXT NOT NULL,
          referrer_host TEXT NOT NULL DEFAULT '',
          is_bot INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_fb_link_clicks_link
          ON fb_link_clicks(link_id, click_day);
        CREATE TABLE IF NOT EXISTS fb_affiliate_commissions (
          id TEXT PRIMARY KEY,
          order_ref TEXT NOT NULL,
          entry_source TEXT NOT NULL,
          page_id TEXT REFERENCES fb_pages(id) ON DELETE SET NULL,
          link_id TEXT REFERENCES fb_affiliate_links(id) ON DELETE SET NULL,
          order_date TEXT NOT NULL,
          amount_vnd INTEGER NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(entry_source, order_ref)
        );
        CREATE TABLE IF NOT EXISTS fb_tracking_settings (
          key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        """)


def validate_target(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in ALLOWED_TARGET_HOSTS:
        raise HTTPException(422, "Chỉ nhận link https thuộc Shopee ("
                            + ", ".join(sorted(ALLOWED_TARGET_HOSTS)) + ").")
    return url


def _new_code(conn) -> str:
    for _ in range(20):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(7))
        if not conn.execute("SELECT 1 FROM fb_affiliate_links WHERE code=?", (code,)).fetchone():
            return code
    raise HTTPException(500, "Không tạo được mã link rút gọn.")


def _hash_salt(conn) -> str:
    row = conn.execute("SELECT value FROM fb_tracking_settings WHERE key='visitor_salt'").fetchone()
    if row:
        return row["value"]
    salt = secrets.token_hex(32)
    conn.execute("INSERT OR IGNORE INTO fb_tracking_settings(key,value) VALUES('visitor_salt',?)", (salt,))
    return conn.execute("SELECT value FROM fb_tracking_settings WHERE key='visitor_salt'").fetchone()["value"]


class LinkCreate(BaseModel):
    label: str = Field(min_length=2, max_length=160)
    target_url: str = Field(min_length=10, max_length=2000)
    page_id: str | None = Field(default=None, max_length=80)
    draft_id: str | None = Field(default=None, max_length=80)
    code: str | None = Field(default=None, max_length=40)


class LinkUpdate(BaseModel):
    enabled: bool | None = None
    label: str | None = Field(default=None, min_length=2, max_length=160)


class CommissionCreate(BaseModel):
    order_ref: str = Field(min_length=1, max_length=120)
    order_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    amount_vnd: int = Field(ge=0, le=1_000_000_000)
    status: Literal["pending", "approved", "cancelled"] = "pending"
    page_id: str | None = Field(default=None, max_length=80)
    link_code: str | None = Field(default=None, max_length=40)
    note: str = Field(default="", max_length=500)


class CommissionImport(BaseModel):
    csv_text: str = Field(min_length=10, max_length=2_000_000)


def _check_refs(conn, page_id: str | None, draft_id: str | None = None) -> None:
    if page_id and not conn.execute("SELECT 1 FROM fb_pages WHERE id=?", (page_id,)).fetchone():
        raise HTTPException(404, "Không tìm thấy Page.")
    if draft_id and not conn.execute("SELECT 1 FROM fb_drafts WHERE id=?", (draft_id,)).fetchone():
        raise HTTPException(404, "Không tìm thấy bản nháp.")


def _link_id_for_code(conn, code: str | None) -> tuple[str | None, str | None]:
    if not code:
        return None, None
    row = conn.execute("SELECT id, page_id FROM fb_affiliate_links WHERE code=?",
                       (code.strip().lower(),)).fetchone()
    if row is None:
        raise HTTPException(404, f"Không tìm thấy link rút gọn '{code}'.")
    return row["id"], row["page_id"]


@router.post("/links")
def create_link(payload: LinkCreate):
    ensure_affiliate_schema()
    target = validate_target(payload.target_url)
    with connect() as conn:
        _check_refs(conn, payload.page_id, payload.draft_id)
        if payload.code:
            code = payload.code.strip().lower()
            if not CODE_PATTERN.match(code):
                raise HTTPException(422, "Mã chỉ gồm chữ thường, số, dấu gạch ngang (4-40 ký tự).")
            if conn.execute("SELECT 1 FROM fb_affiliate_links WHERE code=?", (code,)).fetchone():
                raise HTTPException(409, "Mã link đã tồn tại.")
        else:
            code = _new_code(conn)
        stamp = now()
        link_id = str(uuid.uuid4())
        conn.execute("""INSERT INTO fb_affiliate_links
          (id,code,page_id,draft_id,label,target_url,enabled,created_at,updated_at)
          VALUES(?,?,?,?,?,?,1,?,?)""",
          (link_id, code, payload.page_id, payload.draft_id, payload.label.strip(),
           target, stamp, stamp))
        log(conn, "affiliate.link_created", f"{code}: {payload.label.strip()[:120]}")
    return {"id": link_id, "code": code, "path": f"/go/{code}"}


@router.get("/links")
def list_links():
    ensure_affiliate_schema()
    with connect() as conn:
        rows = conn.execute("""
          SELECT l.*, p.name AS page_name,
            (SELECT COUNT(*) FROM fb_link_clicks c WHERE c.link_id=l.id AND c.is_bot=0) AS clicks,
            (SELECT COUNT(DISTINCT c.click_day || c.visitor_hash) FROM fb_link_clicks c
               WHERE c.link_id=l.id AND c.is_bot=0) AS unique_clicks,
            (SELECT COUNT(*) FROM fb_link_clicks c WHERE c.link_id=l.id AND c.is_bot=1) AS bot_hits
          FROM fb_affiliate_links l LEFT JOIN fb_pages p ON p.id=l.page_id
          ORDER BY l.created_at DESC LIMIT 500
        """).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["path"] = f"/go/{item['code']}"
        items.append(item)
    return {"items": items}


@router.patch("/links/{link_id}")
def update_link(link_id: str, payload: LinkUpdate):
    ensure_affiliate_schema()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_affiliate_links WHERE id=?", (link_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy link.")
        if payload.enabled is not None:
            conn.execute("UPDATE fb_affiliate_links SET enabled=?, updated_at=? WHERE id=?",
                         (int(payload.enabled), now(), link_id))
        if payload.label is not None:
            conn.execute("UPDATE fb_affiliate_links SET label=?, updated_at=? WHERE id=?",
                         (payload.label.strip(), now(), link_id))
    return {"ok": True}


def _upsert_commission(conn, entry_source: str, order_ref: str, order_date: str,
                       amount: int, status: str, page_id: str | None,
                       link_id: str | None, note: str) -> str:
    stamp = now()
    existing = conn.execute(
        "SELECT id FROM fb_affiliate_commissions WHERE entry_source=? AND order_ref=?",
        (entry_source, order_ref)).fetchone()
    if existing:
        conn.execute("""UPDATE fb_affiliate_commissions SET order_date=?, amount_vnd=?,
          status=?, page_id=?, link_id=?, note=?, updated_at=? WHERE id=?""",
          (order_date, amount, status, page_id, link_id, note, stamp, existing["id"]))
        return "updated"
    conn.execute("""INSERT INTO fb_affiliate_commissions
      (id,order_ref,entry_source,page_id,link_id,order_date,amount_vnd,status,note,created_at,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
      (str(uuid.uuid4()), order_ref, entry_source, page_id, link_id, order_date,
       amount, status, note, stamp, stamp))
    return "created"


@router.post("/commissions")
def add_commission(payload: CommissionCreate):
    ensure_affiliate_schema()
    with connect() as conn:
        link_id, link_page = _link_id_for_code(conn, payload.link_code)
        page_id = payload.page_id or link_page
        _check_refs(conn, page_id)
        result = _upsert_commission(conn, "manual", payload.order_ref.strip(), payload.order_date,
                                    payload.amount_vnd, payload.status, page_id, link_id,
                                    payload.note.strip())
        log(conn, "affiliate.commission_" + result, f"{payload.order_ref.strip()[:80]}")
    return {"ok": True, "result": result}


CSV_COLUMNS = ("order_ref", "order_date", "amount_vnd", "status", "link_code")
STATUS_ALIASES = {
    "pending": "pending", "chờ": "pending", "đang chờ": "pending", "chờ xử lý": "pending",
    "approved": "approved", "completed": "approved", "đã duyệt": "approved",
    "hoàn thành": "approved", "cancelled": "cancelled", "canceled": "cancelled",
    "đã hủy": "cancelled", "hủy": "cancelled",
}


def _parse_amount(raw: str) -> int:
    digits = re.sub(r"[^\d]", "", raw or "")
    if not digits:
        raise ValueError("số tiền trống")
    return int(digits)


@router.post("/commissions/import")
def import_commissions(payload: CommissionImport):
    """Import a CSV in the Hub's own template.

    Columns (header row required): order_ref, order_date (YYYY-MM-DD),
    amount_vnd (integer VND), status (pending/approved/cancelled),
    link_code (optional). The affiliate platform's own export format has not
    been verified, so its columns must be mapped to this template first.
    """
    ensure_affiliate_schema()
    reader = csv.DictReader(io.StringIO(payload.csv_text.lstrip("﻿")))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in CSV_COLUMNS[:4] if c not in header]
    if missing:
        raise HTTPException(422, "CSV thiếu cột: " + ", ".join(missing))
    created = updated = 0
    errors: list[str] = []
    with connect() as conn:
        for line_no, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            try:
                order_ref = row["order_ref"]
                if not order_ref:
                    raise ValueError("thiếu order_ref")
                order_date = row["order_date"]
                datetime.strptime(order_date, "%Y-%m-%d")
                amount = _parse_amount(row["amount_vnd"])
                status = STATUS_ALIASES.get(row["status"].casefold())
                if status is None:
                    raise ValueError(f"trạng thái không rõ '{row['status']}'")
                link_id = page_id = None
                if row.get("link_code"):
                    found = conn.execute(
                        "SELECT id, page_id FROM fb_affiliate_links WHERE code=?",
                        (row["link_code"].lower(),)).fetchone()
                    if found is None:
                        raise ValueError(f"không có link '{row['link_code']}'")
                    link_id, page_id = found["id"], found["page_id"]
            except (ValueError, KeyError) as exc:
                if len(errors) < 50:
                    errors.append(f"Dòng {line_no}: {exc}")
                continue
            result = _upsert_commission(conn, "csv", order_ref[:120], order_date, amount,
                                        status, page_id, link_id, "")
            created += result == "created"
            updated += result == "updated"
        log(conn, "affiliate.commissions_imported",
            f"created={created}, updated={updated}, errors={len(errors)}")
    return {"created": created, "updated": updated, "errors": errors}


@router.get("/summary")
def summary(days: int = 30):
    if days < 1 or days > 365:
        raise HTTPException(422, "days phải từ 1 đến 365.")
    ensure_affiliate_schema()
    since_day = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date().isoformat()
    with connect() as conn:
        clicks = conn.execute("""
          SELECT COUNT(*) AS clicks,
                 COUNT(DISTINCT c.link_id || c.click_day || c.visitor_hash) AS unique_clicks
          FROM fb_link_clicks c WHERE c.is_bot=0 AND c.click_day>=?""", (since_day,)).fetchone()
        daily = conn.execute("""
          SELECT click_day AS day, COUNT(*) AS clicks FROM fb_link_clicks
          WHERE is_bot=0 AND click_day>=? GROUP BY click_day ORDER BY click_day""",
          (since_day,)).fetchall()
        commission = {status: 0 for status in COMMISSION_STATUSES}
        orders = {status: 0 for status in COMMISSION_STATUSES}
        for row in conn.execute("""
          SELECT status, SUM(amount_vnd) AS total, COUNT(*) AS n FROM fb_affiliate_commissions
          WHERE order_date>=? GROUP BY status""", (since_day,)):
            commission[row["status"]] = row["total"] or 0
            orders[row["status"]] = row["n"]
        pages = conn.execute("""
          SELECT p.id, p.name,
            (SELECT COUNT(*) FROM fb_link_clicks c JOIN fb_affiliate_links l ON l.id=c.link_id
              WHERE l.page_id=p.id AND c.is_bot=0 AND c.click_day>=?) AS clicks,
            (SELECT COALESCE(SUM(amount_vnd),0) FROM fb_affiliate_commissions m
              WHERE m.page_id=p.id AND m.status='pending' AND m.order_date>=?) AS commission_pending,
            (SELECT COALESCE(SUM(amount_vnd),0) FROM fb_affiliate_commissions m
              WHERE m.page_id=p.id AND m.status='approved' AND m.order_date>=?) AS commission_approved
          FROM fb_pages p ORDER BY p.name""", (since_day, since_day, since_day)).fetchall()
        unassigned = conn.execute("""
          SELECT COALESCE(SUM(amount_vnd),0) FROM fb_affiliate_commissions
          WHERE page_id IS NULL AND status!='cancelled' AND order_date>=?""", (since_day,)).fetchone()[0]
    return {
        "days": days, "since": since_day,
        "clicks": clicks["clicks"], "unique_clicks": clicks["unique_clicks"],
        "daily_clicks": [dict(r) for r in daily],
        "commission_vnd": commission, "orders": orders,
        "commission_unassigned_vnd": unassigned,
        "pages": [dict(r) for r in pages],
        "notice": ("Lượt nhấp đếm qua link rút gọn của Hub, đã loại bot/trình xem trước link. "
                   "Hoa hồng là số liệu nhập tay hoặc từ CSV, cần đối chiếu với trang Affiliate."),
    }


@public_router.get("/go/{code}", include_in_schema=False)
def follow_short_link(code: str, request: Request):
    code = code.strip().lower()
    if not CODE_PATTERN.match(code):
        raise HTTPException(404, "Link không tồn tại.")
    ensure_affiliate_schema()
    with connect() as conn:
        link = conn.execute("SELECT id, target_url, enabled FROM fb_affiliate_links WHERE code=?",
                            (code,)).fetchone()
        if link is None or not link["enabled"]:
            raise HTTPException(404, "Link không tồn tại hoặc đã tắt.")
        # Re-validate at redirect time in case the allow-list was tightened.
        target = validate_target(link["target_url"])
        ua = request.headers.get("user-agent", "")
        ip = request.client.host if request.client else ""
        stamp = now()
        day = stamp[:10]
        visitor = hashlib.sha256(
            f"{_hash_salt(conn)}|{day}|{ip}|{ua}".encode()).hexdigest()[:32]
        referrer_host = (urlsplit(request.headers.get("referer", "")).hostname or "")[:120]
        conn.execute("""INSERT INTO fb_link_clicks
          (id,link_id,clicked_at,click_day,visitor_hash,referrer_host,is_bot)
          VALUES(?,?,?,?,?,?,?)""",
          (str(uuid.uuid4()), link["id"], stamp, day, visitor, referrer_host,
           int(not ua or bool(BOT_UA_PATTERN.search(ua)))))
    response = RedirectResponse(target, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
