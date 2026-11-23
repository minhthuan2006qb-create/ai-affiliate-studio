from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .facebook_hub import connect, init_hub_db, log, now
from .duplicate_guard import find_duplicate


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
          independent_groups INTEGER NOT NULL DEFAULT 1,
          official_sources INTEGER NOT NULL DEFAULT 0,
          conflict_flags TEXT NOT NULL DEFAULT '',
          draft_id TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(item_id, page_id)
        );
        CREATE INDEX IF NOT EXISTS idx_fb_item_processing_created
          ON fb_item_processing(created_at DESC);
        """)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(fb_item_processing)")}
        for column, declaration in {
            "independent_groups": "INTEGER NOT NULL DEFAULT 1",
            "official_sources": "INTEGER NOT NULL DEFAULT 0",
            "conflict_flags": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE fb_item_processing ADD COLUMN {column} {declaration}")


def collect_candidates(conn: sqlite3.Connection, item: sqlite3.Row) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)).isoformat()
    rows = conn.execute("""
      SELECT i.id, i.source_id, i.title, i.item_url, i.summary, i.first_seen_at,
             s.name AS source_name, s.trust_level, s.publisher_group, s.is_official
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


def publisher_group(row: sqlite3.Row | dict) -> str:
    configured = (row["publisher_group"] or "").strip().lower()
    return configured or host_of(row["item_url"])


def detect_numeric_conflicts(item: sqlite3.Row, matched: list[sqlite3.Row]) -> list[str]:
    # Conservative metadata heuristic: flag differing values attached to the same unit.
    # This is a review warning, not semantic fact-checking.
    patterns = [
        re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(%|triệu|tỷ|người|km|giờ|ngày|độ|đồng|lần|ca)\b", re.I),
    ]
    observations: dict[str, set[str]] = {}
    for row in [item, *matched]:
        text = f"{row['title'] or ''} {row['summary'] or ''}".lower()
        for pattern in patterns:
            for value, unit in pattern.findall(text):
                key = unit.lower()
                observations.setdefault(key, set()).add(value.replace(",", "."))
    return [f"Các nguồn nêu số liệu khác nhau cho đơn vị '{unit}': {', '.join(sorted(values))}"
            for unit, values in observations.items() if len(values) > 1]


def publication_age_hours(item: sqlite3.Row) -> float | None:
    raw = item["published_at"] if "published_at" in item.keys() else None
    if not raw:
        return None
    try:
        published = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - published.astimezone(timezone.utc)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def preliminary_signal(item: sqlite3.Row, matched: list[sqlite3.Row]) -> tuple[int, list[str], str, int, int, list[str], bool, bool]:
    rows = [item, *matched]
    hosts = {host_of(row["item_url"]) for row in rows}
    hosts.discard("")
    groups = {publisher_group(row) for row in rows if publisher_group(row)}
    official_source_ids = {row["source_id"] for row in rows
                           if bool(row["is_official"]) or row["trust_level"] == "official"}
    official = len(official_source_ids)
    trusted_groups = {publisher_group(row) for row in rows
                      if publisher_group(row) and
                      (row["trust_level"] in {"official", "established"} or bool(row["is_official"]))}
    score = {0: 0, 1: 35, 2: 55, 3: 70}.get(len(hosts), 75)
    if not (item["summary"] or "").strip():
        score = min(score, 55)
    conflicts = detect_numeric_conflicts(item, matched)
    ages = [publication_age_hours(row) for row in rows]
    stale = any(age is not None and age > WINDOW_HOURS for age in ages)
    future_dated = any(age is not None and age < -6 for age in ages)
    age_hours = publication_age_hours(item)
    if stale:
        conflicts.append("Có ít nhất một nguồn trong cụm tin đã cũ hơn 72 giờ")
    if future_dated:
        conflicts.append("Có ít nhất một nguồn trong cụm tin có thời điểm xuất bản nằm quá 6 giờ trong tương lai")
    reasons = [
        f"{len(hosts)} hostname khác nhau",
        f"{len(groups)} nhóm xuất bản (nhóm khai báo hoặc hostname dự phòng; chưa chứng minh độc lập)",
        f"{len(trusted_groups)} nhóm có mức tin cậy đã cấu hình",
        f"{official} nguồn được đánh dấu chính thức",
        (f"Tuổi tin: khoảng {int(age_hours)} giờ" if age_hours is not None else "Không có thời điểm xuất bản hợp lệ; không thể đánh giá độ mới"),
    ]
    if conflicts:
        reasons.append("CẢNH BÁO MÂU THUẪN: " + "; ".join(conflicts))
    reasons.append("Điểm metadata không phải xác minh sự thật; hostname không chứng minh tính độc lập.")
    rationale = ". ".join(reasons)
    return score, sorted(hosts), rationale, len(trusted_groups), official, conflicts, stale, future_dated


def create_candidate_draft(conn: sqlite3.Connection, item: sqlite3.Row, page_id: str,
                           score: int, hosts: list[str], rationale: str) -> str:
    draft_id = hashlib.sha256(f"draft:{item['id']}:{page_id}".encode()).hexdigest()
    status = "held"  # new candidates require independent trusted groups and no conflict warning
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
          SELECT i.*, s.name AS source_name, s.trust_level, s.publisher_group, s.is_official
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
            score, hosts, rationale, trusted_groups, official_sources, conflicts, stale, future_dated = preliminary_signal(item, matched)
            matched_ids = [row["id"] for row in matched]
            duplicate = find_duplicate(conn, page["id"], item["title"], item["summary"] or item["title"], item["item_url"])
            if duplicate:
                rationale = f"{rationale} Trùng nội dung với bài đã duyệt: {duplicate['title']}. {duplicate['reason']}"
                draft_id = create_candidate_draft(conn, item, page["id"], score, hosts, rationale)
                conn.execute("UPDATE fb_drafts SET review_status='held', review_reason=?, updated_at=? WHERE id=?",
                             (rationale, now(), draft_id))
                record_id = hashlib.sha256(f"process:{item_id}:{page['id']}".encode()).hexdigest()
                conn.execute("""
                  INSERT OR IGNORE INTO fb_item_processing
                  (id,item_id,page_id,signal_score,matched_hosts,rationale,independent_groups,
                   official_sources,conflict_flags,draft_id,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """, (record_id, item_id, page["id"], score, ",".join(hosts), rationale,
                      trusted_groups, official_sources, " | ".join(conflicts), draft_id, now()))
                results.append({"page_id": page["id"], "score": score, "status": "held"})
                created += 1
                continue
            existing = None
            if matched_ids:
                marks = ",".join("?" for _ in matched_ids)
                existing = conn.execute(
                    f"""SELECT x.draft_id FROM fb_item_processing x
                        WHERE x.page_id=? AND x.draft_id IS NOT NULL
                          AND x.item_id IN ({marks}) ORDER BY x.created_at LIMIT 1""",
                    [page["id"], *matched_ids],
                ).fetchone()
            if existing and existing["draft_id"]:
                draft_id = existing["draft_id"]
                draft = conn.execute("SELECT body, review_status FROM fb_drafts WHERE id=?", (draft_id,)).fetchone()
                if draft:
                    sources = [f"{row['source_name']} ({row['item_url']})" for row in matched]
                    sources.append(f"{item['source_name']} ({item['item_url']})")
                    body = draft["body"]
                    for source in sources:
                        if source not in body:
                            body += f"\\nNguồn đối chiếu bổ sung: {source}."
                    status = draft["review_status"]
                    if status in {"pending", "held"}:
                        status = "pending" if score >= 70 and trusted_groups >= 2 and not conflicts and not stale and not future_dated else "held"
                    conn.execute(
                        """UPDATE fb_drafts SET body=?, confidence=?, review_status=?,
                           review_reason=?, updated_at=? WHERE id=?""",
                        (body, score, status, rationale, now(), draft_id),
                    )
            else:
                draft_id = create_candidate_draft(conn, item, page["id"], score, hosts, rationale)
                candidate_status = "pending" if score >= 70 and trusted_groups >= 2 and not conflicts and not stale and not future_dated else "held"
                conn.execute("UPDATE fb_drafts SET review_status=?, review_reason=?, updated_at=? WHERE id=?",
                             (candidate_status, rationale, now(), draft_id))
            record_id = hashlib.sha256(f"process:{item_id}:{page['id']}".encode()).hexdigest()
            final_status = conn.execute("SELECT review_status FROM fb_drafts WHERE id=?", (draft_id,)).fetchone()
            final_status = final_status["review_status"] if final_status else "held"
            conn.execute("""
              INSERT OR IGNORE INTO fb_item_processing
              (id,item_id,page_id,signal_score,matched_hosts,rationale,independent_groups,
               official_sources,conflict_flags,draft_id,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """, (record_id, item_id, page["id"], score, ",".join(hosts), rationale,
                  trusted_groups, official_sources, " | ".join(conflicts), draft_id, now()))
            results.append({"page_id": page["id"], "score": score, "status": final_status})
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
