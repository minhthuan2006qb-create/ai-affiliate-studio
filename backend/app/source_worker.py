from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import socket
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .content_processor import process_unprocessed
from .ai_writer import rewrite_pending
from urllib.parse import urlsplit

import httpx

APP_DIR = Path(__file__).resolve().parent
MAX_FEED_BYTES = 1_000_000
USER_AGENT = "AI-Affiliate-Studio-FeedMonitor/1.0 (+RSS reader)"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path() -> Path:
    return Path(os.getenv("FACEBOOK_HUB_DB", os.getenv("DATABASE_PATH", str(APP_DIR.parent / "data" / "jobs.sqlite3"))))


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS fb_pages (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, meta_page_id TEXT UNIQUE,
          connection_status TEXT NOT NULL DEFAULT 'not_connected',
          enabled INTEGER NOT NULL DEFAULT 1, auto_publish INTEGER NOT NULL DEFAULT 0,
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
        """)
        ensure_source_health_columns(conn)


SOURCE_HEALTH_COLUMNS = {
    "last_poll_status": "TEXT NOT NULL DEFAULT ''",
    "last_error": "TEXT NOT NULL DEFAULT ''",
    "last_success_at": "TEXT",
    "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
}


def ensure_source_health_columns(conn: sqlite3.Connection) -> None:
    """Per-source poll health, used by the Page issues view."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fb_sources)")}
    for name, declaration in SOURCE_HEALTH_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE fb_sources ADD COLUMN {name} {declaration}")


def validate_public_feed_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Chỉ chấp nhận URL RSS dùng HTTP hoặc HTTPS.")
    if parts.username or parts.password:
        raise ValueError("URL nguồn không được chứa thông tin đăng nhập.")
    host = parts.hostname.rstrip(".")
    if host.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError("Không cho phép địa chỉ localhost.")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise ValueError("Không phân giải được tên miền nguồn.") from exc
    if not addresses or any(not addr.is_global for addr in addresses):
        raise ValueError("Nguồn trỏ tới IP nội bộ hoặc IP không công khai; đã chặn để bảo vệ máy chủ.")


def fetch_feed(url: str, client: httpx.Client | None = None) -> bytes:
    validate_public_feed_url(url)
    own_client = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(12.0), follow_redirects=False, headers={"User-Agent": USER_AGENT})
    try:
        with client.stream("GET", url) as response:
            if response.is_redirect:
                raise ValueError("Nguồn RSS chuyển hướng; cần khai báo URL đích trực tiếp.")
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length and int(length) > MAX_FEED_BYTES:
                raise ValueError("Feed vượt giới hạn 1 MB.")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_FEED_BYTES:
                    raise ValueError("Feed vượt giới hạn 1 MB.")
                chunks.append(chunk)
            return b"".join(chunks)
    finally:
        if own_client:
            client.close()


def _child_text(element: ET.Element, names: tuple[str, ...]) -> str:
    for child in list(element):
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in names:
            if local == "link":
                href = child.attrib.get("href")
                if href:
                    rel = child.attrib.get("rel", "alternate")
                    if rel == "alternate":
                        return href.strip()
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""


def parse_feed(xml_bytes: bytes, feed_url: str) -> list[dict[str, str | None]]:
    upper = xml_bytes.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("XML có khai báo DTD/entity không được hỗ trợ.")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError("Nội dung nguồn không phải RSS/Atom XML hợp lệ.") from exc

    items: list[dict[str, str | None]] = []
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1].lower()
        if local not in {"item", "entry"}:
            continue
        title = _child_text(element, ("title",))
        link = _child_text(element, ("link",))
        if not link:
            continue
        if not title:
            title = link
        summary = _child_text(element, ("description", "summary", "content", "encoded"))
        published = _child_text(element, ("pubdate", "published", "updated", "date"))
        if len(summary) > 1200:
            summary = summary[:1200]
        items.append({
            "title": title[:300],
            "url": link[:2048],
            "summary": summary or None,
            "published_at": published[:100] or None,
        })
    return items[:100]


def poll_source(source_id: str, path: Path | None = None, fetcher=fetch_feed) -> dict[str, int | str]:
    ensure_schema(path)
    with connect(path) as conn:
        source = conn.execute("SELECT * FROM fb_sources WHERE id=? AND enabled=1 AND source_type='rss'", (source_id,)).fetchone()
        if source is None:
            return {"source_id": source_id, "status": "skipped", "new_items": 0}
        try:
            raw = fetcher(source["url"])
            items = parse_feed(raw, source["url"])
            inserted = 0
            for item in items:
                key = hashlib.sha256(item["url"].encode("utf-8")).hexdigest()
                item_id = hashlib.sha256(f"{source_id}:{key}".encode("utf-8")).hexdigest()
                cur = conn.execute(
                    """INSERT OR IGNORE INTO fb_source_items
                    (id,source_id,item_key,title,item_url,summary,published_at,first_seen_at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, source_id, key, item["title"], item["url"], item["summary"], item["published_at"], now()),
                )
                inserted += cur.rowcount
            stamp = now()
            conn.execute("""UPDATE fb_sources SET last_checked_at=?, last_poll_status='success',
              last_error='', last_success_at=?, consecutive_failures=0 WHERE id=?""",
              (stamp, stamp, source_id))
            conn.execute(
                "INSERT INTO fb_activity VALUES(?,?,?,?,?)",
                (hashlib.sha256(f"{source_id}:{stamp}".encode()).hexdigest(), "source.polled", "success",
                 f"{source['name']}: {len(items)} entries, {inserted} new", stamp),
            )
            return {"source_id": source_id, "status": "success", "items_seen": len(items), "new_items": inserted}
        except Exception as exc:
            stamp = now()
            conn.execute("""UPDATE fb_sources SET last_checked_at=?, last_poll_status='error',
              last_error=?, consecutive_failures=consecutive_failures+1 WHERE id=?""",
              (stamp, str(exc)[:350], source_id))
            conn.execute(
                "INSERT INTO fb_activity VALUES(?,?,?,?,?)",
                (hashlib.sha256(f"{source_id}:{stamp}:error".encode()).hexdigest(), "source.polled", "error",
                 f"{source['name']}: {str(exc)[:350]}", stamp),
            )
            return {"source_id": source_id, "status": "error", "new_items": 0, "error": str(exc)[:350]}


def poll_all(path: Path | None = None, fetcher=fetch_feed) -> list[dict[str, int | str]]:
    ensure_schema(path)
    with connect(path) as conn:
        rows = conn.execute("SELECT id FROM fb_sources WHERE enabled=1 AND source_type='rss' ORDER BY created_at").fetchall()
    return [poll_source(row["id"], path, fetcher) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Facebook Hub RSS polling worker")
    parser.add_argument("--once", action="store_true", help="Poll all enabled RSS sources once")
    parser.add_argument("--interval-minutes", type=int, default=60, help="Polling interval; default 60 minutes")
    args = parser.parse_args()
    if args.interval_minutes < 5:
        parser.error("--interval-minutes không được nhỏ hơn 5 để tránh gây tải nguồn.")
    ensure_schema()
    print(f"RSS worker started; interval={args.interval_minutes} minutes", flush=True)
    while True:
        results = poll_all()
        processed = process_unprocessed()
        rewritten = rewrite_pending()
        print({"checked": len(results), "results": results, "processed": processed, "rewritten": rewritten}, flush=True)
        if args.once:
            break
        time.sleep(args.interval_minutes * 60)


if __name__ == "__main__":
    main()
