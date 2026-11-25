"""Public, read-only website for approved Hub articles.

Rules enforced here (keep them when editing):
- Only drafts a human reviewer approved (or that were published) are shown.
  Revoking approval makes the page disappear (404).
- "Đọc bài gốc" links point straight to the original source URL. They never
  go through an affiliate or tracking redirect.
- Shopee products are shown in a separate, clearly labelled block after the
  original-source links; readers choose whether to click. Those links use the
  Hub short links (/go/{code}) so clicks can be counted.
- All text is HTML-escaped; only http(s) URLs are turned into links.
"""
from __future__ import annotations

import html
import os
import re
import unicodedata
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from .facebook_hub import connect, init_hub_db

router = APIRouter(prefix="/site", tags=["Public website"], include_in_schema=False)

PUBLIC_STATUSES = ("approved", "published")
SITE_NAME = os.getenv("PUBLIC_SITE_NAME", "Bản tin tổng hợp")
# Everything from these markers onward in an AI-written body is reviewer/internal
# material (source list, "not independently verified" note) and is rendered
# separately or dropped on the public page.
INTERNAL_MARKERS = ("\n\nNguồn tham khảo (liên kết gốc):", "\n\nLưu ý: Bản viết lại bằng AI")
URL_RE = re.compile(r"https?://[^\s<>\"']+")
CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

STYLE = """
:root{--bg:#fbfaf7;--text:#1d232b;--muted:#5f6b78;--line:#e3e0d8;--accent:#0b6b55;--card:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#11161c;--text:#e9edf2;--muted:#9aa7b4;--line:#27313c;--accent:#6fd3b4;--card:#18202a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:17px/1.65 Georgia,'Noto Serif',serif}
header,main,footer{max-width:720px;margin:auto;padding:16px}header a{color:var(--text);text-decoration:none;font:700 18px system-ui,sans-serif}
h1{font-size:26px;line-height:1.3;margin:8px 0}h2{font:700 16px system-ui,sans-serif;margin:28px 0 10px}
.meta,.note,footer{color:var(--muted);font:13px/1.5 system-ui,sans-serif}a{color:var(--accent);overflow-wrap:anywhere}
.btn{display:inline-block;margin:6px 0;padding:11px 16px;border-radius:10px;background:var(--accent);color:var(--bg);font:700 15px system-ui,sans-serif;text-decoration:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:10px 0}
.list a{font:600 17px system-ui,sans-serif;text-decoration:none;color:var(--text)}
.products{border-top:1px solid var(--line);margin-top:28px}.products a{font:600 15px system-ui,sans-serif}
"""


SLUG_ID_LENGTH = 10


def slugify(title: str, max_length: int = 80) -> str:
    """Vietnamese title -> ASCII slug: 'Đà Nẵng mở lễ hội' -> 'da-nang-mo-le-hoi'."""
    text = (title or "").replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > max_length:
        text = text[:max_length].rsplit("-", 1)[0]
    return text or "bai-viet"


def short_id(draft_id: str) -> str:
    return draft_id.replace("-", "").lower()[:SLUG_ID_LENGTH]


def article_path(draft_id: str, title: str) -> str:
    return f"/site/a/{slugify(title)}-{short_id(draft_id)}"


def _esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def _safe_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme in ("http", "https") and parts.hostname:
        return url.strip()
    return None


def _linkify(text: str) -> str:
    out, pos = [], 0
    for match in URL_RE.finditer(text):
        out.append(_esc(text[pos:match.start()]))
        url = match.group(0).rstrip(".,;:)")
        tail = match.group(0)[len(url):]
        out.append(f'<a href="{_esc(url)}" rel="noopener nofollow" target="_blank">{_esc(url)}</a>{_esc(tail)}')
        pos = match.end()
    out.append(_esc(text[pos:]))
    return "".join(out)


def public_body(body: str) -> str:
    cut = len(body)
    for marker in INTERNAL_MARKERS:
        index = body.find(marker)
        if index != -1:
            cut = min(cut, index)
    return body[:cut].strip()


def _paragraphs(text: str) -> str:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return "".join("<p>" + _linkify(b).replace("\n", "<br>") + "</p>" for b in blocks)


def _summary(text: str, limit: int = 180) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[:limit].rsplit(" ", 1)[0] + "…"


def _page(title: str, body_html: str, description: str = "", og_type: str = "website") -> HTMLResponse:
    doc = f"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><meta name="description" content="{_esc(description)}">
<meta property="og:title" content="{_esc(title)}"><meta property="og:description" content="{_esc(description)}">
<meta property="og:type" content="{og_type}"><style>{STYLE}</style></head><body>
<header><a href="/site/">{_esc(SITE_NAME)}</a></header><main>{body_html}</main>
<footer>Bài viết là bản tóm tắt được biên tập với sự hỗ trợ của AI và đã qua người kiểm duyệt.
Thông tin đầy đủ và chính xác nhất nằm ở bài gốc. Một số liên kết sản phẩm là liên kết tiếp thị;
trang có thể nhận hoa hồng khi bạn mua hàng, giá bạn trả không đổi.</footer></body></html>"""
    response = HTMLResponse(doc)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


def _article_list(rows) -> str:
    if not rows:
        return '<p class="note">Chưa có bài viết.</p>'
    return "".join(
        f'<div class="card list"><a href="{_esc(article_path(r["id"], r["title"]))}">{_esc(r["title"])}</a>'
        f'<div class="meta">{_esc(r["page_name"])} · {_esc((r["updated_at"] or "")[:10])}</div>'
        f'<div class="note">{_esc(_summary(public_body(r["body"])))}</div></div>'
        for r in rows)


def _list_query(where: str = "", params: tuple = ()) -> list:
    init_hub_db()
    with connect() as conn:
        return conn.execute(f"""
          SELECT d.id, d.title, d.body, d.updated_at, p.name AS page_name
          FROM fb_drafts d JOIN fb_pages p ON p.id=d.page_id
          WHERE d.review_status IN ('approved','published') AND p.enabled=1 {where}
          ORDER BY d.updated_at DESC LIMIT 100""", params).fetchall()


@router.get("/", response_class=HTMLResponse)
def site_home():
    rows = _list_query()
    return _page(SITE_NAME, "<h1>Bài mới</h1>" + _article_list(rows),
                 "Tin tóm tắt đã qua kiểm duyệt, kèm liên kết bài gốc.")


@router.get("/p/{page_id}", response_class=HTMLResponse)
def site_page(page_id: str):
    init_hub_db()
    with connect() as conn:
        page = conn.execute("SELECT id, name FROM fb_pages WHERE id=? AND enabled=1",
                            (page_id,)).fetchone()
    if page is None:
        raise HTTPException(404, "Không tìm thấy chuyên mục.")
    rows = _list_query("AND d.page_id=?", (page_id,))
    return _page(f"{page['name']} · {SITE_NAME}",
                 f"<h1>{_esc(page['name'])}</h1>" + _article_list(rows))


def _original_sources(conn, draft) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    url = _safe_url(draft["source_url"])
    if url:
        found[url] = draft["source_name"] or urlsplit(url).hostname or url
    has_processing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fb_item_processing'").fetchone()
    if has_processing:
        for row in conn.execute("""
          SELECT DISTINCT i.item_url, s.name FROM fb_item_processing pr
          JOIN fb_source_items i ON i.id=pr.item_id JOIN fb_sources s ON s.id=i.source_id
          WHERE pr.draft_id=? ORDER BY i.first_seen_at""", (draft["id"],)):
            url = _safe_url(row["item_url"])
            if url and url not in found:
                found[url] = row["name"]
    return list(found.items())


def _products(conn, draft_id: str) -> list:
    has_links = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fb_affiliate_links'").fetchone()
    if not has_links:
        return []
    return conn.execute("""SELECT code, label FROM fb_affiliate_links
      WHERE draft_id=? AND enabled=1 ORDER BY created_at LIMIT 6""", (draft_id,)).fetchall()


PUBLIC_DRAFT_SQL = """
  SELECT d.*, p.name AS page_name, p.id AS pid FROM fb_drafts d
  JOIN fb_pages p ON p.id=d.page_id
  WHERE d.review_status IN ('approved','published') AND p.enabled=1 AND {cond}"""


def _find_public_draft(conn, key: str):
    """Accept the canonical '<slug>-<short id>' path or a bare draft id (old links)."""
    row = conn.execute(PUBLIC_DRAFT_SQL.format(cond="d.id=?"), (key,)).fetchone()
    if row is not None:
        return row
    tail = key.lower().rsplit("-", 1)[-1]
    if not re.fullmatch(r"[0-9a-z]{1,%d}" % SLUG_ID_LENGTH, tail):
        return None
    rows = conn.execute(
        PUBLIC_DRAFT_SQL.format(cond="lower(substr(replace(d.id,'-',''),1,?))=?") + " LIMIT 2",
        (SLUG_ID_LENGTH, tail)).fetchall()
    return rows[0] if len(rows) == 1 else None


@router.get("/a/{key}", response_class=HTMLResponse)
def site_article(key: str):
    init_hub_db()
    with connect() as conn:
        draft = _find_public_draft(conn, key)
        if draft is None:
            raise HTTPException(404, "Bài viết không tồn tại hoặc chưa được duyệt.")
        canonical = article_path(draft["id"], draft["title"])
        if f"/site/a/{key}" != canonical:
            # Old id links or an outdated slug after a title edit.
            return RedirectResponse(canonical, status_code=301)
        draft_id = draft["id"]
        sources = _original_sources(conn, draft)
        products = _products(conn, draft_id)

    text = public_body(draft["body"])
    parts = [
        f'<div class="meta"><a href="/site/p/{_esc(draft["pid"])}">{_esc(draft["page_name"])}</a>'
        f' · {_esc((draft["updated_at"] or "")[:10])}</div>',
        f"<h1>{_esc(draft['title'])}</h1>",
        _paragraphs(text),
    ]
    if sources:
        first_url, first_name = sources[0]
        parts.append(
            f'<p><a class="btn" href="{_esc(first_url)}" rel="noopener" target="_blank">'
            f'Đọc bài gốc tại {_esc(first_name)}</a></p>')
        if len(sources) > 1:
            parts.append("<h2>Các nguồn khác</h2><ul>" + "".join(
                f'<li><a href="{_esc(u)}" rel="noopener" target="_blank">{_esc(n)}</a></li>'
                for u, n in sources[1:]) + "</ul>")
    else:
        parts.append('<p class="note">Bài này chưa có liên kết bài gốc.</p>')
    if products:
        parts.append(
            '<section class="products"><h2>Sản phẩm liên quan trên Shopee</h2>'
            '<p class="note">Liên kết tiếp thị: trang có thể nhận hoa hồng nếu bạn mua hàng. '
            'Bạn không cần nhấp để đọc bài.</p>' + "".join(
                f'<div class="card"><a href="/go/{_esc(p["code"])}" rel="sponsored nofollow noopener" '
                f'target="_blank">{_esc(p["label"])}</a></div>' for p in products)
            + "</section>")
    return _page(draft["title"], "".join(parts), _summary(text), "article")
