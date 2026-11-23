from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from .facebook_hub import connect, log, now, require_hub_key

router = APIRouter(prefix="/api/facebook-hub/meta", tags=["Facebook Meta OAuth"])
GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v27.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
DIALOG_BASE = "https://www.facebook.com"
SCOPES = "pages_show_list,pages_read_engagement,pages_manage_posts"


def _config() -> tuple[str, str, str, str]:
    app_id = os.getenv("META_APP_ID", "").strip()
    app_secret = os.getenv("META_APP_SECRET", "").strip()
    redirect_uri = os.getenv("META_REDIRECT_URI", "").strip()
    encryption_key = os.getenv("META_TOKEN_ENCRYPTION_KEY", "").strip()
    if not all((app_id, app_secret, redirect_uri, encryption_key)):
        raise HTTPException(
            503,
            "Meta OAuth chưa cấu hình đủ META_APP_ID, META_APP_SECRET, META_REDIRECT_URI và META_TOKEN_ENCRYPTION_KEY.",
        )
    if not redirect_uri.startswith("https://") and "localhost" not in redirect_uri:
        raise HTTPException(503, "META_REDIRECT_URI phải dùng HTTPS (trừ môi trường localhost).")
    try:
        Fernet(encryption_key.encode("ascii"))
    except Exception:
        raise HTTPException(503, "META_TOKEN_ENCRYPTION_KEY không phải Fernet key hợp lệ.")
    return app_id, app_secret, redirect_uri, encryption_key


def _cipher() -> Fernet:
    _, _, _, key = _config()
    return Fernet(key.encode("ascii"))


def encrypt_token(token: str) -> str:
    return _cipher().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_token(token_ciphertext: str) -> str:
    try:
        return _cipher().decrypt(token_ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        raise HTTPException(500, "Không thể giải mã thông tin kết nối Meta.")


def init_meta_oauth_db() -> None:
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS fb_meta_oauth_states (
          state_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fb_meta_page_tokens (
          page_id TEXT PRIMARY KEY REFERENCES fb_pages(id) ON DELETE CASCADE,
          meta_page_id TEXT NOT NULL UNIQUE,
          token_ciphertext TEXT NOT NULL,
          granted_scopes TEXT NOT NULL DEFAULT '',
          connected_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """)


def _exchange_code(code: str, app_id: str, app_secret: str, redirect_uri: str) -> str:
    try:
        with httpx.Client(timeout=15.0, follow_redirects=False) as client:
            response = client.get(f"{GRAPH_BASE}/oauth/access_token", params={
                "client_id": app_id, "client_secret": app_secret,
                "redirect_uri": redirect_uri, "code": code,
            })
            response.raise_for_status()
            token = response.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise ValueError("Missing access token")
            return token
    except (httpx.HTTPError, ValueError, KeyError):
        raise HTTPException(502, "Meta không cấp được access token. Hãy kiểm tra cấu hình OAuth và thử lại.")


def _fetch_pages(user_token: str) -> list[dict]:
    pages: list[dict] = []
    url = f"{GRAPH_BASE}/me/accounts"
    params = {"fields": "id,name,access_token,tasks", "limit": "100", "access_token": user_token}
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            for _ in range(10):
                response = client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                for item in payload.get("data", []):
                    if item.get("id") and item.get("name") and item.get("access_token"):
                        pages.append(item)
                next_url = payload.get("paging", {}).get("next")
                if not next_url:
                    break
                # Never follow pagination URLs to a host other than Meta Graph.
                if not next_url.startswith(GRAPH_BASE + "/"):
                    break
                url, params = next_url, {}
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Không thể lấy danh sách Page từ Meta. Kiểm tra quyền pages_show_list.")
    return pages


@router.on_event("startup")
def startup_meta_oauth() -> None:
    init_meta_oauth_db()


@router.get("/connect")
def begin_meta_oauth():
    app_id, _, redirect_uri, _ = _config()
    state = secrets.token_urlsafe(32)
    stamp = datetime.now(timezone.utc)
    with connect() as conn:
        conn.execute("DELETE FROM fb_meta_oauth_states WHERE expires_at < ?", (stamp.isoformat(),))
        conn.execute("INSERT INTO fb_meta_oauth_states(state_hash,created_at,expires_at) VALUES(?,?,?)",
                     (hashlib.sha256(state.encode()).hexdigest(), stamp.isoformat(),
                      (stamp + timedelta(minutes=10)).isoformat()))
    params = urlencode({
        "client_id": app_id, "redirect_uri": redirect_uri, "state": state,
        "response_type": "code", "scope": SCOPES,
    })
    response = RedirectResponse(f"{DIALOG_BASE}/{GRAPH_VERSION}/dialog/oauth?{params}", status_code=302)
    response.set_cookie("fb_meta_oauth_state", state, max_age=600, httponly=True,
                        secure=redirect_uri.startswith("https://"), samesite="lax",
                        path="/api/facebook-hub/meta")
    return response


@router.get("/callback")
def finish_meta_oauth(
    request: Request,
    code: str | None = Query(default=None, max_length=4096),
    state: str | None = Query(default=None, max_length=256),
    error: str | None = Query(default=None, max_length=120),
):
    if error:
        raise HTTPException(400, "Bạn đã hủy hoặc Meta từ chối quyền kết nối.")
    if not code or not state:
        raise HTTPException(400, "Thiếu mã xác thực hoặc state OAuth.")
    browser_state = request.cookies.get("fb_meta_oauth_state", "")
    if not browser_state or not hmac.compare_digest(browser_state, state):
        raise HTTPException(400, "OAuth state không khớp phiên trình duyệt. Hãy bắt đầu kết nối lại.")
    app_id, app_secret, redirect_uri, _ = _config()
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    stamp = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        row = conn.execute("SELECT expires_at FROM fb_meta_oauth_states WHERE state_hash=?",
                           (state_hash,)).fetchone()
        # Consume state exactly once, even when it has expired.
        conn.execute("DELETE FROM fb_meta_oauth_states WHERE state_hash=?", (state_hash,))
    if not row or row["expires_at"] < stamp:
        raise HTTPException(400, "OAuth state không hợp lệ hoặc đã hết hạn. Hãy bắt đầu kết nối lại.")

    user_token = _exchange_code(code, app_id, app_secret, redirect_uri)
    pages = _fetch_pages(user_token)
    if not pages:
        raise HTTPException(403, "Không tìm thấy Page có quyền phù hợp trong tài khoản này.")
    connected, skipped = [], []
    with connect() as conn:
        for item in pages:
            tasks = set(item.get("tasks") or [])
            if "CREATE_CONTENT" not in tasks and "MANAGE" not in tasks:
                skipped.append(item["name"])
                continue
            existing = conn.execute("SELECT id FROM fb_pages WHERE meta_page_id=?",
                                     (item["id"],)).fetchone()
            if existing:
                page_id = existing["id"]
                conn.execute("UPDATE fb_pages SET name=?, connection_status='connected', updated_at=? WHERE id=?",
                             (item["name"], now(), page_id))
            else:
                import uuid
                page_id = str(uuid.uuid4())
                stamp_now = now()
                conn.execute("""INSERT INTO fb_pages
                    (id,name,meta_page_id,connection_status,created_at,updated_at)
                    VALUES(?,?,?,'connected',?,?)""",
                    (page_id, item["name"], item["id"], stamp_now, stamp_now))
            conn.execute("""INSERT INTO fb_meta_page_tokens
                (page_id,meta_page_id,token_ciphertext,granted_scopes,connected_at,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(page_id) DO UPDATE SET meta_page_id=excluded.meta_page_id,
                  token_ciphertext=excluded.token_ciphertext,granted_scopes=excluded.granted_scopes,
                  updated_at=excluded.updated_at""",
                (page_id, item["id"], encrypt_token(item["access_token"]), SCOPES, now(), now()))
            connected.append({"id": page_id, "name": item["name"]})
        log(conn, "meta.oauth_connected", f"connected={len(connected)}; skipped={len(skipped)}")
    from fastapi.responses import JSONResponse
    response = JSONResponse({"ok": True, "connected_pages": connected, "skipped_pages": skipped,
            "message": "Đã lưu token Page ở dạng mã hóa phía máy chủ. Chưa tự đăng bài."})
    response.delete_cookie("fb_meta_oauth_state", path="/api/facebook-hub/meta")
    return response


@router.get("/status", dependencies=[Depends(require_hub_key)])
def meta_connection_status():
    with connect() as conn:
        rows = conn.execute("""
          SELECT p.id,p.name,p.meta_page_id,p.connection_status,t.granted_scopes,t.connected_at
          FROM fb_pages p LEFT JOIN fb_meta_page_tokens t ON t.page_id=p.id
          ORDER BY p.name
        """).fetchall()
    return {"pages": [dict(row) for row in rows]}


@router.delete("/pages/{page_id}", dependencies=[Depends(require_hub_key)])
def disconnect_meta_page(page_id: str):
    with connect() as conn:
        row = conn.execute("SELECT id FROM fb_pages WHERE id=?", (page_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Không tìm thấy Page.")
        conn.execute("DELETE FROM fb_meta_page_tokens WHERE page_id=?", (page_id,))
        conn.execute("""UPDATE fb_pages SET connection_status='not_connected',
          auto_publish=0, updated_at=? WHERE id=?""", (now(), page_id))
        log(conn, "meta.page_disconnected", page_id)
    return {"ok": True, "auto_publish": False}
