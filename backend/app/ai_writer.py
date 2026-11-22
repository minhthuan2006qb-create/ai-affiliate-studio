from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from urllib.parse import urlsplit

import httpx

from .facebook_hub import connect, init_hub_db, log, now


def configured() -> bool:
    return bool(os.getenv("AI_API_BASE_URL", "").strip()
                and os.getenv("AI_API_KEY", "").strip()
                and os.getenv("AI_MODEL", "").strip())


def ensure_writer_schema() -> None:
    init_hub_db()
    with connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(fb_drafts)")}
        if "writer_status" not in columns:
            conn.execute("ALTER TABLE fb_drafts ADD COLUMN writer_status TEXT NOT NULL DEFAULT 'not_started'")
        if "writer_model" not in columns:
            conn.execute("ALTER TABLE fb_drafts ADD COLUMN writer_model TEXT")
        if "writer_notes" not in columns:
            conn.execute("ALTER TABLE fb_drafts ADD COLUMN writer_notes TEXT")


def endpoint() -> str:
    base = os.getenv("AI_API_BASE_URL", "").strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1"}):
        raise ValueError("AI_API_BASE_URL phải dùng HTTPS (chỉ cho phép HTTP với localhost).")
    if not parts.netloc or parts.username or parts.password:
        raise ValueError("AI_API_BASE_URL không hợp lệ.")
    return base + "/chat/completions"


def plain_text(value: str, limit: int) -> str:
    value = re.sub(r"<[^>]{0,200}>", " ", str(value or ""))
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit]


def parse_response(data: dict) -> tuple[str, str, str]:
    try:
        raw = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("AI provider trả về cấu trúc không hợp lệ.") from exc
    if isinstance(raw, list):
        raw = "".join(part.get("text", "") for part in raw if isinstance(part, dict))
    raw = str(raw).strip()
    raw = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw, flags=re.I)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("AI provider không trả về JSON hợp lệ.") from exc
    title = plain_text(result.get("title", ""), 220)
    body = plain_text(result.get("body", ""), 5000)
    notes = plain_text(result.get("notes", ""), 700)
    if len(title) < 5 or len(body) < 40:
        raise ValueError("Bản viết lại quá ngắn hoặc thiếu tiêu đề.")
    return title, body, notes or "AI đã biên tập; các dữ kiện vẫn cần con người kiểm tra."


def rewrite_draft(draft_id: str, client: httpx.Client | None = None) -> dict[str, str]:
    ensure_writer_schema()
    if not configured():
        return {"draft_id": draft_id, "status": "skipped", "reason": "Chưa cấu hình AI_API_BASE_URL, AI_API_KEY và AI_MODEL."}
    with connect() as conn:
        draft = conn.execute("SELECT * FROM fb_drafts WHERE id=?", (draft_id,)).fetchone()
        if draft is None:
            return {"draft_id": draft_id, "status": "missing"}
        if draft["review_status"] in {"approved", "rejected", "published"}:
            return {"draft_id": draft_id, "status": "skipped", "reason": "Bản nháp đã được duyệt/từ chối/xuất bản; không tự sửa."}
        sources = conn.execute("""
          SELECT DISTINCT s.name AS source_name, i.title, i.summary, i.item_url
          FROM fb_item_processing x
          JOIN fb_source_items i ON i.id=x.item_id
          JOIN fb_sources s ON s.id=i.source_id
          WHERE x.draft_id=? ORDER BY s.name LIMIT 12
        """, (draft_id,)).fetchall()
    if not sources:
        return {"draft_id": draft_id, "status": "skipped", "reason": "Không tìm thấy dữ liệu RSS gốc để biên tập."}
    source_text = []
    for index, source in enumerate(sources, 1):
        source_text.append({
            "source_number": index,
            "publisher": plain_text(source["source_name"], 120),
            "headline": plain_text(source["title"], 300),
            "excerpt": plain_text(source["summary"] or "", 1200),
        })
    prompt = {
        "task": "Biên tập một bản tin tiếng Việt ngắn, rõ ràng, trung lập cho Facebook.",
        "rules": [
            "Chỉ dùng dữ kiện được nêu rõ trong các tiêu đề và trích yếu đầu vào.",
            "Không suy đoán nguyên nhân, con số, thời gian, địa điểm, nhân vật hoặc kết quả còn thiếu.",
            "Nếu các nguồn không đủ thông tin, nói rõ thông tin đang được cập nhật/chưa đủ căn cứ; không tự lấp chỗ trống.",
            "Không biến lời đồn, dự kiến hoặc nhận định thành sự thật.",
            "Không sao chép nguyên văn dài; diễn đạt lại ngắn gọn.",
            "Không tạo URL, tên nguồn, trích dẫn hoặc chi tiết mới.",
            "Trả về duy nhất JSON với các khóa title, body, notes; không markdown fence.",
        ],
        "sources": source_text,
    }
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(35.0), follow_redirects=False)
    try:
        response = client.post(
            endpoint(),
            headers={"Authorization": "Bearer " + os.getenv("AI_API_KEY", "").strip(),
                     "Content-Type": "application/json"},
            json={
                "model": os.getenv("AI_MODEL", "").strip(),
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": "Bạn là biên tập viên tin tức thận trọng. Không được bịa dữ kiện."},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        title, body, notes = parse_response(response.json())
        refs = "\n\nNguồn tham khảo (liên kết gốc):\n" + "\n".join(
            f"- {plain_text(row['source_name'], 120)}: {row['item_url']}" for row in sources
        )
        body = body + refs + (
            "\n\nLưu ý: Bản viết lại bằng AI từ trích yếu RSS; chưa được xác minh độc lập. "
            "Cần người kiểm tra nội dung và nguồn trước khi đăng."
        )
        model = os.getenv("AI_MODEL", "").strip()[:120]
        with connect() as conn:
            current = conn.execute("SELECT review_status FROM fb_drafts WHERE id=?", (draft_id,)).fetchone()
            if current is None or current["review_status"] in {"approved", "rejected", "published"}:
                return {"draft_id": draft_id, "status": "skipped", "reason": "Trạng thái bản nháp đã thay đổi trong lúc xử lý."}
            conn.execute("""
              UPDATE fb_drafts SET title=?, body=?, writer_status='done',
                writer_model=?, writer_notes=?, review_reason=?, updated_at=?
              WHERE id=?
            """, (title, body, model, notes, "AI đã biên tập; chưa xác minh độc lập. " + notes, now(), draft_id))
            log(conn, "draft.ai_rewritten", f"{draft_id}: model={model}; human review required")
        return {"draft_id": draft_id, "status": "done", "model": model}
    except Exception as exc:
        with connect() as conn:
            conn.execute("UPDATE fb_drafts SET writer_status='error', writer_notes=?, updated_at=? WHERE id=?",
                         (str(exc)[:700], now(), draft_id))
            log(conn, "draft.ai_rewrite_failed", f"{draft_id}: {str(exc)[:300]}", status="error")
        return {"draft_id": draft_id, "status": "error", "reason": str(exc)[:300]}
    finally:
        if own:
            client.close()


def rewrite_pending(limit: int = 5) -> list[dict[str, str]]:
    if limit < 1 or limit > 20:
        raise ValueError("limit must be between 1 and 20")
    ensure_writer_schema()
    if not configured():
        return []
    with connect() as conn:
        rows = conn.execute("""
          SELECT id FROM fb_drafts
          WHERE review_status IN ('pending','held')
            AND COALESCE(writer_status,'not_started') IN ('not_started','error')
          ORDER BY created_at LIMIT ?
        """, (limit,)).fetchall()
    return [rewrite_draft(row["id"]) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Optional AI rewrite worker for Facebook Hub drafts")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    results = rewrite_pending(args.limit)
    print({"rewritten": len([r for r in results if r["status"] == "done"]), "results": results}, flush=True)


if __name__ == "__main__":
    main()
