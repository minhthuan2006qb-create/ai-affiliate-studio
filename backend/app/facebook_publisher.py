from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends

from .facebook_hub import connect, log, now, require_hub_key
from .facebook_meta import GRAPH_BASE, _config, decrypt_token, init_meta_oauth_db
from .facebook_scheduler import draft_content_hash, init_scheduler_db
from .facebook_token_health import init_token_health_db
from .facebook_hub import init_hub_db
from .facebook_token_health import _evaluate, inspect_token
from .duplicate_guard import find_duplicate

router = APIRouter(prefix="/api/facebook-hub/meta", tags=["Facebook Meta Publisher"])
PUBLISH_ENABLED = os.getenv("META_PUBLISHING_ENABLED", "").strip().lower() in {"1", "true", "yes"}
REQUIRED_SCOPES = {"pages_show_list", "pages_read_engagement", "pages_manage_posts"}


def init_publisher_db() -> None:
    init_hub_db()
    init_meta_oauth_db()
    init_scheduler_db()
    init_token_health_db()
    with connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(fb_publish_schedule)")}
        for name, declaration in {
            "publish_attempt_id": "TEXT",
            "publish_attempted_at": "TEXT",
            "published_post_id": "TEXT",
            "publish_error": "TEXT",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE fb_publish_schedule ADD COLUMN {name} {declaration}")


def _claim_due_item(schedule_id: str) -> dict | None:
    """Atomically reserve a ready item. A reserved item is never auto-retried."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
          SELECT q.*,d.title,d.body,d.source_url,d.review_status,d.page_id AS draft_page_id,
                 p.name AS page_name,p.meta_page_id,p.enabled AS page_enabled,
                 p.auto_publish, p.connection_status,
                 t.token_ciphertext,t.granted_scopes
          FROM fb_publish_schedule q
          JOIN fb_drafts d ON d.id=q.draft_id
          JOIN fb_pages p ON p.id=q.page_id
          LEFT JOIN fb_meta_page_tokens t ON t.page_id=p.id
          WHERE q.id=? AND q.state='ready'
        """, (schedule_id,)).fetchone()
        if row is None:
            return None
        reason = None
        if datetime.fromisoformat(row["planned_at"]) > datetime.now(timezone.utc):
            reason = "Chưa đến giờ đăng."
        elif not PUBLISH_ENABLED:
            reason = "META_PUBLISHING_ENABLED chưa được bật ở backend."
        elif not row["page_enabled"] or not row["auto_publish"]:
            reason = "Page đang tắt hoặc chưa bật tự động đăng."
        elif row["connection_status"] != "connected" or not row["meta_page_id"] or not row["token_ciphertext"]:
            reason = "Page chưa có kết nối Meta hợp lệ."
        elif row["review_status"] != "approved" or row["draft_page_id"] != row["page_id"]:
            reason = "Bản nháp không còn được duyệt hoặc không thuộc Page này."
        elif not row["content_hash"] or draft_content_hash(row) != row["content_hash"]:
            reason = "Nội dung đã thay đổi sau khi duyệt/xếp lịch."
        else:
            duplicate = find_duplicate(conn, row["page_id"], row["title"], row["body"],
                                       row["source_url"], exclude_draft_id=row["draft_id"])
            if duplicate:
                reason = "Phát hiện nội dung có khả năng trùng bài đã đăng."
        if reason:
            conn.execute("UPDATE fb_publish_schedule SET state='blocked',publish_error=?,updated_at=? WHERE id=?",
                         (reason[:500], now(), schedule_id))
            log(conn, "meta.publish_blocked", f"{schedule_id}: {reason}", "warning")
            return None
        attempt_id = str(uuid.uuid4())
        stamp = now()
        conn.execute("""
          UPDATE fb_publish_schedule SET state='publishing',publish_attempt_id=?,
            publish_attempted_at=?,publish_error=NULL,updated_at=?
          WHERE id=? AND state='ready'
        """, (attempt_id, stamp, stamp, schedule_id))
        if conn.total_changes < 1:
            return None
        log(conn, "meta.publish_claimed", f"schedule={schedule_id}; attempt={attempt_id}")
        result = dict(row)
        result["publish_attempt_id"] = attempt_id
        return result


def _preflight_and_publish(item: dict) -> dict:
    """Freshly checks token and Page task before the one permitted Graph API POST."""
    schedule_id = item["id"]
    try:
        app_id, app_secret, _, _ = _config()
        token = decrypt_token(item["token_ciphertext"])
        token_data = inspect_token(token)
        health, health_message, expires_at, data_expires = _evaluate(token_data)
        scopes = set(token_data.get("scopes") or [])
        if token_data.get("app_id") and str(token_data["app_id"]) != app_id:
            raise RuntimeError("Token thuộc ứng dụng Meta khác.")
        if health != "healthy" or not REQUIRED_SCOPES.issubset(scopes):
            raise RuntimeError("Token không đạt kiểm tra mới nhất: " + health_message)
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            page_response = client.get(
                f"{GRAPH_BASE}/{item['meta_page_id']}",
                params={"fields": "id,tasks", "access_token": token},
            )
            page_response.raise_for_status()
            page_data = page_response.json()
            if str(page_data.get("id")) != str(item["meta_page_id"]):
                raise RuntimeError("Meta trả về Page ID không khớp.")
            if "CREATE_CONTENT" not in set(page_data.get("tasks") or []):
                raise RuntimeError("Token hiện không có tác vụ CREATE_CONTENT trên Page.")
            message = item["title"].strip() + "\n\n" + item["body"].strip()
            if item.get("source_url"):
                message += "\n\nNguồn tham khảo: " + item["source_url"].strip()
            response = client.post(
                f"{GRAPH_BASE}/{item['meta_page_id']}/feed",
                data={"message": message, "access_token": token},
            )
            response.raise_for_status()
            payload = response.json()
        post_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(post_id, str) or not post_id:
            # The request may have succeeded even if the response was malformed.
            raise TimeoutError("Meta không trả về post ID; cần đối soát thủ công, không tự gửi lại.")
        with connect() as conn:
            conn.execute("""
              UPDATE fb_publish_schedule SET state='published',published_post_id=?,
                publish_error=NULL,updated_at=? WHERE id=? AND state='publishing'
                AND publish_attempt_id=?
            """, (post_id, now(), schedule_id, item["publish_attempt_id"]))
            conn.execute("""
              UPDATE fb_meta_page_tokens SET health_status='healthy',health_message=?,
                last_checked_at=?,expires_at=?,data_access_expires_at=? WHERE page_id=?
            """, (health_message, now(), expires_at, data_expires, item["page_id"]))
            log(conn, "meta.publish_succeeded", f"schedule={schedule_id}; post_id={post_id}")
        return {"schedule_id": schedule_id, "state": "published", "post_id": post_id}
    except (httpx.TimeoutException, httpx.TransportError, TimeoutError) as exc:
        # A timeout/connection loss after POST is ambiguous. Never retry automatically:
        # Facebook may already have created the post.
        message = "Kết quả gửi không xác định; cần kiểm tra Page trước khi xử lý lại."
        with connect() as conn:
            conn.execute("""UPDATE fb_publish_schedule SET state='unknown',publish_error=?,
              updated_at=? WHERE id=? AND state='publishing' AND publish_attempt_id=?""",
              (message, now(), schedule_id, item["publish_attempt_id"]))
            log(conn, "meta.publish_unknown", f"schedule={schedule_id}; {type(exc).__name__}", "warning")
        return {"schedule_id": schedule_id, "state": "unknown", "message": message}
    except Exception as exc:
        message = str(exc)[:450] or "Lỗi không xác định khi đăng."
        with connect() as conn:
            conn.execute("""UPDATE fb_publish_schedule SET state='failed',publish_error=?,
              updated_at=? WHERE id=? AND state='publishing' AND publish_attempt_id=?""",
              (message, now(), schedule_id, item["publish_attempt_id"]))
            log(conn, "meta.publish_failed", f"schedule={schedule_id}; {message}", "error")
        return {"schedule_id": schedule_id, "state": "failed", "message": message}


def process_ready_items(limit: int = 10) -> dict:
    init_publisher_db()
    if not PUBLISH_ENABLED:
        return {"processed": 0, "disabled": True,
                "note": "META_PUBLISHING_ENABLED chưa bật; không thay đổi lịch và không gửi bài."}
    with connect() as conn:
        # A process crash after claiming an item leaves an ambiguous result.
        # Convert it to unknown instead of risking a duplicate post on restart.
        stale_cutoff = (datetime.now(timezone.utc) - __import__('datetime').timedelta(minutes=2)).isoformat()
        stale = conn.execute("""
          UPDATE fb_publish_schedule SET state='unknown',
            publish_error='Worker dừng khi đang gửi; cần đối soát thủ công.',
            updated_at=?
          WHERE state='publishing' AND (publish_attempted_at IS NULL OR publish_attempted_at < ?)
        """, (now(), stale_cutoff)).rowcount
        rows = conn.execute("""
          SELECT id FROM fb_publish_schedule WHERE state='ready'
          ORDER BY planned_at LIMIT ?
        """, (max(1, min(int(limit), 50)),)).fetchall()
    results = []
    for row in rows:
        claimed = _claim_due_item(row["id"])
        if claimed:
            results.append(_preflight_and_publish(claimed))
    return {"processed": len(results), "interrupted_marked_unknown": stale, "items": results}


@router.get("/publish/status", dependencies=[Depends(require_hub_key)])
def publish_status(limit: int = 50):
    if limit < 1 or limit > 200:
        from fastapi import HTTPException
        raise HTTPException(422, "limit phải từ 1 đến 200.")
    init_publisher_db()
    with connect() as conn:
        rows = conn.execute("""
          SELECT q.id,q.page_id,p.name AS page_name,q.draft_id,d.title,q.planned_at,
                 q.state,q.publish_attempted_at,q.published_post_id,q.publish_error
          FROM fb_publish_schedule q JOIN fb_pages p ON p.id=q.page_id
          JOIN fb_drafts d ON d.id=q.draft_id
          WHERE q.state IN ('publishing','published','failed','unknown','blocked')
          ORDER BY q.updated_at DESC LIMIT ?
        """, (limit,)).fetchall()
    return {"publishing_enabled": PUBLISH_ENABLED, "items": [dict(row) for row in rows],
            "note": "Trạng thái unknown cần kiểm tra Page thủ công; hệ thống không tự gửi lại."}


def main() -> None:
    parser = argparse.ArgumentParser(description="Facebook Hub Meta publishing worker")
    parser.add_argument("--once", action="store_true", help="Process ready posts once and exit")
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    if args.interval_seconds < 10:
        parser.error("--interval-seconds tối thiểu 10.")
    init_publisher_db()
    if not PUBLISH_ENABLED:
        print("Publishing disabled: set META_PUBLISHING_ENABLED=true to allow live posts.", flush=True)
        if args.once:
            return
    while True:
        try:
            result = process_ready_items(args.limit)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"status": "error", "message": str(exc)[:300]}, ensure_ascii=False), flush=True)
        if args.once:
            break
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
