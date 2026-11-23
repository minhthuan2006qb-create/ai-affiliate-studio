from __future__ import annotations

import re
import sqlite3
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STOPWORDS = {"va", "la", "cua", "cho", "voi", "tai", "tren", "duoi", "mot", "nhung", "cac", "da", "se", "bi", "duoc", "theo", "tu", "den", "khi", "trong", "tin", "moi", "the", "and", "of"}

def normalize_url(value: str | None) -> str:
    if not value:
        return ""
    try:
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}]
        path = re.sub(r"/+", "/", parts.path or "/").rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))
    except (TypeError, ValueError):
        return ""

def tokens(value: str) -> set[str]:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"[^0-9a-zA-ZÀ-ỹ]+", " ", value)
    return {x for x in value.split() if len(x) > 1 and x not in STOPWORDS}

def similarity(a: str, b: str) -> float:
    left, right = tokens(a), tokens(b)
    return len(left & right) / len(left | right) if left and right else 0.0

def is_material_update(candidate: str, previous: str) -> bool:
    """Allow a same-story revision only when it adds a meaningful amount of new information."""
    new_tokens, old_tokens = tokens(candidate), tokens(previous)
    if not new_tokens or not old_tokens:
        return False
    novel = new_tokens - old_tokens
    return len(novel) >= 5 and len(novel) / max(len(new_tokens), 1) >= 0.25

def ensure_history_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
      CREATE TABLE IF NOT EXISTS fb_page_content_history (
        id TEXT PRIMARY KEY,
        page_id TEXT NOT NULL REFERENCES fb_pages(id) ON DELETE CASCADE,
        draft_id TEXT NOT NULL UNIQUE REFERENCES fb_drafts(id) ON DELETE CASCADE,
        canonical_url TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        approved_at TEXT NOT NULL
      )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_page_history_page ON fb_page_content_history(page_id, approved_at DESC)")

def find_duplicate(conn: sqlite3.Connection, page_id: str, title: str, body: str, source_url: str | None = None, exclude_draft_id: str | None = None) -> dict | None:
    ensure_history_schema(conn)
    canonical = normalize_url(source_url)
    rows = conn.execute("""
      SELECT h.*, d.title AS draft_title
      FROM fb_page_content_history h
      LEFT JOIN fb_drafts d ON d.id=h.draft_id
      WHERE h.page_id=? AND h.draft_id<>COALESCE(?, '')
      ORDER BY h.approved_at DESC LIMIT 300
    """, (page_id, exclude_draft_id)).fetchall()
    candidate = f"{title or ''} {body or ''}"
    for row in rows:
        previous = f"{row['title']} {row['body']}"
        same_url = bool(canonical and row["canonical_url"] and canonical == row["canonical_url"])
        if same_url and not is_material_update(candidate, previous):
            return {"draft_id": row["draft_id"], "title": row["title"], "reason": "Đường dẫn nguồn đã được dùng; nội dung chưa có đủ thông tin mới để xem là bản cập nhật."}
        score = similarity(candidate, previous)
        if score >= 0.68 and not is_material_update(candidate, previous):
            return {"draft_id": row["draft_id"], "title": row["title"], "reason": f"Nội dung gần trùng bài đã duyệt (độ tương đồng {round(score * 100)}%)."}
    return None

def remember_approved(conn: sqlite3.Connection, draft: sqlite3.Row, approved_at: str) -> None:
    ensure_history_schema(conn)
    conn.execute("""
      INSERT INTO fb_page_content_history(id,page_id,draft_id,canonical_url,title,body,approved_at)
      VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(draft_id) DO UPDATE SET canonical_url=excluded.canonical_url,
        title=excluded.title, body=excluded.body, approved_at=excluded.approved_at
    """, (f"history:{draft['id']}", draft["page_id"], draft["id"],
          normalize_url(draft["source_url"]), draft["title"], draft["body"], approved_at))
