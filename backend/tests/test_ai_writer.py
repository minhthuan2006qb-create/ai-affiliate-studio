from __future__ import annotations

import json

from app import ai_writer as aw
from app import facebook_hub as hub
from app import content_processor as cp


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": json.dumps({
            "title": "Lễ hội pháo hoa tại Đà Nẵng",
            "body": "Một số nguồn RSS đang đề cập đến lễ hội pháo hoa tại Đà Nẵng. Hiện cần kiểm tra thêm thời gian và lịch tổ chức.",
            "notes": "Chưa có lịch tổ chức cụ thể trong trích yếu.",
        }, ensure_ascii=False)}}]}


class FakeClient:
    def __init__(self):
        self.payload = None

    def post(self, url, **kwargs):
        self.payload = kwargs
        return FakeResponse()


def test_ai_writer_skips_without_credentials(monkeypatch):
    monkeypatch.delenv("AI_API_BASE_URL", raising=False)
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)
    assert aw.rewrite_pending() == []


def test_ai_writer_requires_secure_provider_endpoint(monkeypatch):
    monkeypatch.setenv("AI_API_BASE_URL", "http://example.com/v1")
    monkeypatch.setenv("AI_API_KEY", "test")
    monkeypatch.setenv("AI_MODEL", "test-model")
    try:
        aw.endpoint()
        assert False, "Expected HTTPS validation"
    except ValueError:
        pass


def test_ai_rewrite_keeps_source_links_and_requires_human_review(tmp_path, monkeypatch):
    path = tmp_path / "hub.sqlite3"
    monkeypatch.setattr(hub, "DB_PATH", path)
    hub.init_hub_db()
    with hub.connect() as conn:
        stamp = hub.now()
        conn.execute("INSERT INTO fb_pages(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                     ("p1", "Tin tức Đà Nẵng", stamp, stamp))
        conn.execute("""UPDATE fb_pages SET editorial_tone=?, editorial_audience=?,
          editorial_topics=?, editorial_avoid=?, editorial_guidance=? WHERE id=?""",
          ("Gần gũi, ngắn gọn", "Người dân Đà Nẵng", "Giao thông\\nSự kiện địa phương",
           "Tin đồn chưa xác minh", "Ưu tiên thông tin thiết thực.", "p1"))
        conn.execute("INSERT INTO fb_sources(id,name,url,source_type,created_at) VALUES(?,?,?,?,?)",
                     ("s1", "Nguồn A", "https://source.example/rss", "rss", stamp))
        conn.execute("INSERT INTO fb_page_sources(page_id,source_id,created_at) VALUES(?,?,?)",
                     ("p1", "s1", stamp))
        conn.execute("""INSERT INTO fb_source_items
          (id,source_id,item_key,title,item_url,summary,first_seen_at)
          VALUES(?,?,?,?,?,?,?)""",
          ("i1", "s1", "k1", "Đà Nẵng tổ chức lễ hội pháo hoa",
           "https://source.example/story", "Một trích yếu có thông tin sự kiện.", stamp))
    cp.process_item("i1")
    with hub.connect() as conn:
        draft_id = conn.execute("SELECT id FROM fb_drafts").fetchone()["id"]
        # Even a candidate that passed preliminary metadata checks must be held
        # after AI rewriting until a separate evidence check is implemented.
        conn.execute("UPDATE fb_drafts SET review_status='pending' WHERE id=?", (draft_id,))
    monkeypatch.setenv("AI_API_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("AI_API_KEY", "secret-test")
    monkeypatch.setenv("AI_MODEL", "test-model")
    client = FakeClient()
    result = aw.rewrite_draft(draft_id, client=client)
    assert result["status"] == "done"
    assert result["evidence_status"] == "insufficient"
    assert result["evidence_claims"] == "0"
    assert client.payload["json"]["messages"][1]["content"].find("Không suy đoán") >= 0
    prompt = json.loads(client.payload["json"]["messages"][1]["content"])
    assert prompt["page_editorial_profile"]["tone"] == "Gần gũi, ngắn gọn"
    assert "Giao thông" in prompt["page_editorial_profile"]["topics"]
    with hub.connect() as conn:
        draft = conn.execute("SELECT * FROM fb_drafts WHERE id=?", (draft_id,)).fetchone()
        assert draft["writer_status"] == "done"
        assert "https://source.example/story" in draft["body"]
        assert "chưa được xác minh độc lập" in draft["body"]
        assert draft["review_status"] == "held"
