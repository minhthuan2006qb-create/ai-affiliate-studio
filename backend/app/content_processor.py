from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .facebook_hub import connect, init_hub_db, log, now


WINDOW_HOURS = 72
MIN_TITLE_SIMILARITY = 0.45
STOPWORDS = {
    "va", "la", "cua", "cho", "voi", "tai", "tren", "duoi", "mot", "nhung",
    "cac", "da", "se", "bi", "duoc", "theo", "tu", "den", "khi", "trong",
    "tin", "moi", "news", "the", "and", "of", "to", "in", "on", "for",
}


def tokens(text: str) -> set[str]:
    normalized = text.lower()
    normalized = re.sub(r"[^0-9a-zA-ZÀ-ỹ]+", " ", normalized)
    return {word for word in normalized.split() if len(word) > 1 and word not in STOPWORDS}


def similarity(left: str, right: str) -> float:
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def ensure_processing_schema() -> None:
    init_hub_db()
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS fb_item_processing (
          id TEXT PRIMARY KEY,
          item_id TEXT NOT NULL REFERENCES fb_source_items(id) ON DELETE CASCADE,
          page_id TEXT NOT NULL REFERENCES fb_pages(id) ON DELETE CASCADE,
          signal_score INTEGER NOT NULL,
          matched_hosts TEXT NOT NULL,
          rationale TEXT NOT NULL,
          draft_id TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(item_id, page_id)
        );
        CREATE INDEX IF NOT EXISTS idx_fb_item_processing_created
          ON fb_item_processing(created_at DESC);
        """)


def collect_candidates(conn: sqlite3.Connection, item: sqlite3.Row) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).isoformat()
    rows = conn.execute("""
      SELECT i.id, i.title, i.item_url, i.summary, i.first_seen_at, s.name AS source_name
      FROM fb_source_items i
      JOIN fb_sources s ON s.id=i.source_id
      JOIN fb_page_sources ps ON ps.source_id=i.source_id
      WHERE ps.page_id=? AND ps.enabled=1 AND s.enabled=1
        AND i.first_seen_at>=? AND i.id<>?
      ORDER BY i.first_seen_at DESC LIMIT 300
    """, (item["page_id"], cutoff, item["id"])).fetchall()
    matched = []
    for row in rows:
        if similarity(item["title"], row["title"]) >= MIN_TITLE_SIMILARITY:
            matched.append(row)
    return matched


def preliminary_signal(item: sqlite3.Row, matched: list[sqlite3.Row]) -> tuple[int, list[str], str]:
    hosts = {host_of(item["item_url"])}
    for row in matched:
        host = host_of(row["item_url"])
        if host:
            hosts.add(host)
    hosts.discard("")
    count = len(hosts)
    # This is metadata coverage only. It is deliberately not presented as fact-checking.
    score = {0: 0, 1: 35, 2: 55, 3: 70}.get(count, 75)
    has_summary = bool((item["summary"] or "").strip())
    if not has_summary:
        score = min(score, 55)
    rationale = f"Ghi nhận {count} hostname khác nhau trong cửa sổ {WINDOW_HOURS} giờ; "
    rationale += "có trích yếu RSS." if has_summary else "thiếu trích yếu RSS."
    rationale += " Điểm này chỉ đo độ phủ metadata, không xác minh sự thật hoặc tính độc lập của nguồn."
    return score, sorted(hosts), rationale


def create_candidate_draft(conn: sqlite3.Connection, item: sqlite3.Row, page_id: str,
                           score: int, hosts: list[str], rationale: str) -> str:
    draft_id = hashlib.sha256(f"draft:{item['id']}:{page_id}".encode()).hexdigest()
    status = "pending" if score >= 70 else "held"
    source_name = item["source_name"]
    source_url = item["item_url"]
    summary = (item["summary"] or "").strip()
    body_text = summary if summary else item["title"]
    body = (
        f"{body_text}\n\n"
        f"Nguồn tham khảo: {source_name} ({source_url}).\n"
        f"Ghi chú hệ thống: {rationale} "
        "Đây là bản nháp thu thập thông tin, chưa được xác minh độc lập; "
        "không dùng làm thông báo sự kiện hoặc tự động xuất bản."
    )
    stamp = now()
    conn.execute("""
      INSERT OR IGNORE INTO fb_drafts
      (id,page_id,source_name,source_url,title,body,confidence,review_status,
       review_reason,created_at,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?)
    """, (draft_id, page_id, source_name, source_url,
          f"Thông tin cần kiểm chứng: {item['title']}", body, score, status,
          rationale, stamp, stamp))
    return draft_id


def process_item(item_id: str) -> dict[str, object]:
    ensure_processing_schema()
    created = 0
    with connect() as conn:
        item = conn.execute("""
          SELECT i.*, s.name AS source_name
          FROM fb_source_items i JOIN fb_sources s ON s.id=i.source_id
          WHERE i.id=?
        """, (item_id,)).fetchone()
        if item is None:
            return {"item_id": item_id, "status": "missing", "drafts_created": 0}
        pages = conn.execute("""
          SELECT p.id FROM fb_pages p
          JOIN fb_page_sources ps ON ps.page_id=p.id
          WHERE ps.source_id=? AND ps.enabled=1 AND p.enabled=1
        """, (item["source_id"],)).fetchall()
        results = []
        for page in pages:
            if conn.execute("SELECT 1 FROM fb_item_processing WHERE item_id=? AND page_id=?",
                            (item_id, page["id"])).fetchone():
                continue
            matched = collect_candidates(conn, {**dict(item), "page_id": page["id"]})
            score, hosts, rationale = preliminary_signal(item, matched)
            draft_id = create_candidate_draft(conn, item, page["id"], score, hosts, rationale)
            record_id = hashlib.sha256(f"process:{item_id}:{page['id']}".encode()).hexdigest()
            conn.execute("""
              INSERT OR IGNORE INTO fb_item_processing
              (id,item_id,page_id,signal_score,matched_hosts,rationale,draft_id,created_at)
              VALUES(?,?,?,?,?,?,?,?)
            """, (record_id, item_id, page["id"], score, ",".join(hosts), rationale, draft_id, now()))
            results.append({"page_id": page["id"], "score": score,
                            "status": "pending" if score >= 70 else "held"})
            created += 1
        if created:
            log(conn, "source_item.processed",
                f"{item_id}: created {created} candidate draft(s); preliminary metadata signal only")
    return {"item_id": item_id, "status": "processed", "drafts_created": created, "results": results}


def process_unprocessed(limit: int = 100) -> list[dict[str, object]]:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    ensure_processing_schema()
    with connect() as conn:
        rows = conn.execute("""
          SELECT i.id FROM fb_source_items i
          WHERE EXISTS (
            SELECT 1 FROM fb_page_sources ps
            JOIN fb_pages p ON p.id=ps.page_id
            WHERE ps.source_id=i.source_id AND ps.enabled=1 AND p.enabled=1
          )
          AND NOT EXISTS (SELECT 1 FROM fb_item_processing x WHERE x.item_id=i.id)
          ORDER BY i.first_seen_at ASC LIMIT ?
        """, (limit,)).fetchall()
    return [process_item(row["id"]) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Facebook Hub candidate triage worker")
    parser.add_argument("--once", action="store_true", help="Process currently unprocessed feed items once")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    results = process_unprocessed(args.limit)
    print({"processed_items": len(results), "results": results}, flush=True)


if __name__ == "__main__":
    main()
