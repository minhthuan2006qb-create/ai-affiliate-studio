from __future__ import annotations

import argparse
import json
import time
import httpx
from fastapi import APIRouter, Depends, HTTPException

from .facebook_hub import connect, log, now, require_hub_key
from .facebook_meta import GRAPH_BASE, _config, decrypt_token

router = APIRouter(prefix="/api/facebook-hub/meta", tags=["Facebook Meta Token Health"])
REQUIRED_SCOPES = {"pages_show_list", "pages_read_engagement", "pages_manage_posts"}
EXPIRY_WARNING_SECONDS = 7 * 24 * 60 * 60


def init_token_health_db() -> None:
    with connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(fb_meta_page_tokens)")}
        for name, declaration in {
            "health_status": "TEXT NOT NULL DEFAULT 'unchecked'",
            "health_message": "TEXT NOT NULL DEFAULT ''",
            "last_checked_at": "TEXT",
            "expires_at": "INTEGER",
            "data_access_expires_at": "INTEGER",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE fb_meta_page_tokens ADD COLUMN {name} {declaration}")


def inspect_token(page_token: str) -> dict:
    app_id, app_secret, _, _ = _config()
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            response = client.get(
                f"{GRAPH_BASE}/debug_token",
                params={"input_token": page_token, "access_token": f"{app_id}|{app_secret}"},
            )
            response.raise_for_status()
            data = response.json().get("data", {})
        if not isinstance(data, dict) or "is_valid" not in data:
            raise ValueError("Invalid debug_token response")
        return data
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(502, "Meta không phản hồi được kết quả kiểm tra token. Có thể thử lại sau.")


def _unix_or_none(value) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _evaluate(data: dict) -> tuple[str, str, int | None, int | None]:
    expires_at = _unix_or_none(data.get("expires_at"))
    data_expires = _unix_or_none(data.get("data_access_expires_at"))
    if not data.get("is_valid"):
        return "invalid", "Meta xác nhận token không còn hợp lệ. Cần kết nối lại Page.", expires_at, data_expires
    scopes = set(data.get("scopes") or [])
    missing = REQUIRED_SCOPES - scopes
    if missing:
        return "missing_permissions", "Token thiếu quyền: " + ", ".join(sorted(missing)), expires_at, data_expires
    now_ts = int(time.time())
    deadlines = [v for v in (expires_at, data_expires) if v]
    if deadlines and min(deadlines) <= now_ts:
        return "invalid", "Token hoặc quyền truy cập dữ liệu đã hết hạn. Cần kết nối lại.", expires_at, data_expires
    if deadlines and min(deadlines) - now_ts <= EXPIRY_WARNING_SECONDS:
        return "expiring", "Token hoặc quyền truy cập dữ liệu sẽ hết hạn trong 7 ngày.", expires_at, data_expires
    return "healthy", "Meta xác nhận token hợp lệ và đủ các quyền cần thiết tại thời điểm kiểm tra.", expires_at, data_expires


@router.on_event("startup")
def startup_token_health() -> None:
    init_token_health_db()


@router.get("/health", dependencies=[Depends(require_hub_key)])
def get_token_health():
    with connect() as conn:
        rows = conn.execute("""
            SELECT p.id AS page_id,p.name,p.connection_status,
                   t.health_status,t.health_message,t.last_checked_at,
                   t.expires_at,t.data_access_expires_at,t.granted_scopes
            FROM fb_pages p LEFT JOIN fb_meta_page_tokens t ON t.page_id=p.id
            ORDER BY p.name
        """).fetchall()
    return {"pages": [dict(row) for row in rows]}


def check_all_tokens():
    with connect() as conn:
        rows = conn.execute("""
            SELECT p.id AS page_id,p.name,t.token_ciphertext
            FROM fb_pages p JOIN fb_meta_page_tokens t ON t.page_id=p.id
            ORDER BY p.name
        """).fetchall()
    results = []
    for row in rows:
        checked_at = now()
        try:
            token = decrypt_token(row["token_ciphertext"])
            data = inspect_token(token)
            status, message, expires_at, data_expires = _evaluate(data)
            with connect() as conn:
                conn.execute("""
                    UPDATE fb_meta_page_tokens
                    SET health_status=?,health_message=?,last_checked_at=?,
                        expires_at=?,data_access_expires_at=?
                    WHERE page_id=?
                """, (status, message, checked_at, expires_at, data_expires, row["page_id"]))
                if status in {"invalid", "missing_permissions"}:
                    conn.execute("""UPDATE fb_pages SET connection_status='needs_reconnect',
                        auto_publish=0,updated_at=? WHERE id=?""", (checked_at, row["page_id"]))
                elif status in {"healthy", "expiring"}:
                    conn.execute("""UPDATE fb_pages SET connection_status='connected',
                        updated_at=? WHERE id=?""", (checked_at, row["page_id"]))
                log(conn, "meta.token_health_checked", f"page={row['page_id']}; status={status}")
        except HTTPException as exc:
            status, message, expires_at, data_expires = "check_failed", exc.detail, None, None
            with connect() as conn:
                conn.execute("""UPDATE fb_meta_page_tokens SET health_status=?,
                    health_message=?,last_checked_at=? WHERE page_id=?""",
                    (status, message, checked_at, row["page_id"]))
                log(conn, "meta.token_health_check_failed", f"page={row['page_id']}", "warning")
        results.append({"page_id": row["page_id"], "page_name": row["name"],
                        "health_status": status, "health_message": message,
                        "last_checked_at": checked_at, "expires_at": expires_at,
                        "data_access_expires_at": data_expires})
    return {"checked": len(results), "pages": results}


@router.post("/health/check", dependencies=[Depends(require_hub_key)])
def check_token_health():
    return check_all_tokens()


def main() -> None:
    parser = argparse.ArgumentParser(description="Facebook Hub Meta token health monitor")
    parser.add_argument("--once", action="store_true", help="Check all connected Page tokens once")
    parser.add_argument("--interval-hours", type=float, default=12.0,
                        help="Background check interval in hours (minimum 1)")
    args = parser.parse_args()
    if args.interval_hours < 1:
        parser.error("--interval-hours không được nhỏ hơn 1 giờ.")
    init_token_health_db()
    interval = args.interval_hours * 3600
    print(f"Meta token health monitor started; interval={args.interval_hours:g} hours", flush=True)
    while True:
        try:
            result = check_all_tokens()
            print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"status": "error", "message": str(exc)[:300]}, ensure_ascii=False), flush=True)
        if args.once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
