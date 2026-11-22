from __future__ import annotations

import sqlite3
from pathlib import Path

from app import source_worker as sw


RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>Tin thu nhat</title><link>https://example.org/a</link><description>Mo ta ngan</description><pubDate>Mon, 23 Nov 2026 10:00:00 GMT</pubDate></item>
<item><title>Tin thu hai</title><link>https://example.org/b</link></item>
</channel></rss>"""


def seed_source(path: Path):
    sw.ensure_schema(path)
    with sw.connect(path) as conn:
        conn.execute("INSERT INTO fb_sources(id,name,url,source_type,created_at) VALUES(?,?,?,?,?)",
                     ("s1", "Feed demo", "https://example.org/rss", "rss", sw.now()))
        conn.execute("INSERT INTO fb_pages(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                     ("p1", "Page demo", sw.now(), sw.now()))
        conn.execute("INSERT INTO fb_page_sources(page_id,source_id,created_at) VALUES(?,?,?)",
                     ("p1", "s1", sw.now()))


def test_parse_rss_extracts_metadata_without_full_article():
    items = sw.parse_feed(RSS, "https://example.org/rss")
    assert len(items) == 2
    assert items[0]["title"] == "Tin thu nhat"
    assert items[0]["url"] == "https://example.org/a"
    assert items[0]["summary"] == "Mo ta ngan"


def test_poll_source_deduplicates_items(tmp_path):
    path = tmp_path / "hub.sqlite3"
    seed_source(path)
    fetcher = lambda url: RSS
    first = sw.poll_source("s1", path, fetcher)
    second = sw.poll_source("s1", path, fetcher)
    assert first["new_items"] == 2
    assert second["new_items"] == 0
    with sw.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fb_source_items").fetchone()[0] == 2


def test_poll_source_rejects_non_public_feed_url(tmp_path):
    path = tmp_path / "hub.sqlite3"
    seed_source(path)
    with sw.connect(path) as conn:
        conn.execute("UPDATE fb_sources SET url=? WHERE id='s1'", ("http://127.0.0.1/feed",))
    result = sw.poll_source("s1", path)
    assert result["status"] == "error"
    assert "IP" in result["error"] or "localhost" in result["error"]


def test_atom_feed_parsing():
    atom = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom entry</title><link href="https://example.org/story"/><summary>Brief</summary><updated>2026-11-23T08:00:00Z</updated></entry></feed>'''
    items = sw.parse_feed(atom, "https://example.org/atom.xml")
    assert len(items) == 1
    assert items[0]["url"] == "https://example.org/story"
    assert items[0]["summary"] == "Brief"
