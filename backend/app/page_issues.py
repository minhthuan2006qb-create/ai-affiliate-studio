"""Collects everything a Page operator should look at, in one list.

Read-only: this module never changes drafts, sources or tokens. It only
summarises states already recorded by the evidence checker, source worker,
token-health worker and publisher, so it cannot mark anything as resolved.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from .facebook_hub import connect, init_hub_db, require_hub_key
from .source_worker import ensure_source_health_columns

router = APIRouter(prefix="/api/facebook-hub", tags=["Facebook Hub issues"],
                   dependencies=[Depends(require_hub_key)])

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
STALE_SOURCE_HOURS = 48
SOURCE_FAILURES_CRITICAL = 3
MAX_DRAFT_ISSUES = 200


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ts(value: str | None) -> float:
    try:
        return datetime.fromisoformat(value).timestamp() if value else 0.0
    except (TypeError, ValueError):
        return 0.0


def _issue(page, category, severity, title, detail, ref_type="", ref_id="", at=None):
    return {"page_id": page["id"] if page else None,
            "page_name": page["name"] if page else "Không gắn Page",
            "category": category, "severity": severity, "title": title,
            "detail": detail, "ref_type": ref_type, "ref_id": ref_id, "at": at}


def collect_issues(page_id: str | None = None) -> list[dict]:
    init_hub_db()
    issues: list[dict] = []
    with connect() as conn:
        ensure_source_health_columns(conn)
        pages = conn.execute(
            "SELECT * FROM fb_pages" + (" WHERE id=?" if page_id else "") + " ORDER BY name",
            (page_id,) if page_id else ()).fetchall()
        if page_id and not pages:
            raise HTTPException(404, "Không tìm thấy Page.")
        page_by_id = {p["id"]: p for p in pages}
        ids = list(page_by_id)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))

        # 1. Content held back or flagged by the evidence ledger.
        has_evidence = _table_exists(conn, "fb_evidence_claims")
        evidence_sql = """,
            (SELECT COUNT(*) FROM fb_evidence_claims c WHERE c.draft_id=d.id AND c.status='unsupported') AS unsupported,
            (SELECT COUNT(*) FROM fb_evidence_claims c WHERE c.draft_id=d.id AND c.status='contradicted') AS contradicted
        """ if has_evidence else ", 0 AS unsupported, 0 AS contradicted"
        drafts = conn.execute(f"""
          SELECT d.id, d.page_id, d.title, d.review_status, d.review_reason, d.updated_at {evidence_sql}
          FROM fb_drafts d
          WHERE d.page_id IN ({marks}) AND d.review_status IN ('held','pending')
          ORDER BY d.updated_at DESC LIMIT ?""", (*ids, MAX_DRAFT_ISSUES)).fetchall()
        for d in drafts:
            page = page_by_id[d["page_id"]]
            if d["contradicted"]:
                issues.append(_issue(page, "content", "critical",
                    f"Bài có dữ kiện mâu thuẫn: {d['title']}",
                    f"{d['contradicted']} dữ kiện có giá trị khác nhau giữa các nguồn tin cậy. "
                    "Cần mở bài gốc trước khi duyệt.", "draft", d["id"], d["updated_at"]))
            elif d["unsupported"]:
                issues.append(_issue(page, "content", "warning",
                    f"Bài có chi tiết không có trong nguồn: {d['title']}",
                    f"{d['unsupported']} tên/địa điểm/trích dẫn/số liệu không tìm thấy trong "
                    "dữ liệu RSS; có thể do AI tự thêm.", "draft", d["id"], d["updated_at"]))
            elif d["review_status"] == "held":
                issues.append(_issue(page, "content", "info",
                    f"Bài đang giữ lại: {d['title']}",
                    d["review_reason"] or "Chờ người kiểm tra.", "draft", d["id"], d["updated_at"]))

        # 2. Source health for sources assigned to each Page.
        stale_before = (datetime.now(timezone.utc) - timedelta(hours=STALE_SOURCE_HOURS)).isoformat()
        sources = conn.execute(f"""
          SELECT ps.page_id, s.id, s.name, s.source_type, s.enabled, s.last_poll_status,
                 s.last_error, s.last_success_at, s.last_checked_at, s.consecutive_failures
          FROM fb_page_sources ps JOIN fb_sources s ON s.id=ps.source_id
          WHERE ps.page_id IN ({marks}) AND ps.enabled=1""", ids).fetchall()
        sources_per_page = {pid: 0 for pid in ids}
        for s in sources:
            page = page_by_id[s["page_id"]]
            if not s["enabled"]:
                continue
            sources_per_page[s["page_id"]] += 1
            if s["source_type"] != "rss":
                continue
            if s["last_poll_status"] == "error":
                failures = s["consecutive_failures"] or 0
                issues.append(_issue(page, "source",
                    "critical" if failures >= SOURCE_FAILURES_CRITICAL else "warning",
                    f"Nguồn lỗi: {s['name']}",
                    f"Lỗi {failures} lần liên tiếp. Lỗi gần nhất: {s['last_error'] or 'không rõ'}",
                    "source", s["id"], s["last_checked_at"]))
            elif not s["last_success_at"]:
                issues.append(_issue(page, "source", "info",
                    f"Nguồn chưa từng lấy tin thành công: {s['name']}",
                    "Worker đọc RSS có thể chưa chạy hoặc nguồn mới được thêm.",
                    "source", s["id"], s["last_checked_at"]))
            elif s["last_success_at"] < stale_before:
                issues.append(_issue(page, "source", "warning",
                    f"Nguồn không cập nhật hơn {STALE_SOURCE_HOURS} giờ: {s['name']}",
                    f"Lần lấy tin thành công gần nhất: {s['last_success_at']}. "
                    "Kiểm tra worker hoặc nguồn.", "source", s["id"], s["last_success_at"]))
        for pid, count in sources_per_page.items():
            page = page_by_id[pid]
            if page["enabled"] and count == 0:
                issues.append(_issue(page, "source", "info", "Page chưa có nguồn tin nào đang bật",
                                     "Gán ít nhất một nguồn cho Page.", "page", pid))

        # 3. Facebook connection and token health.
        tokens = {}
        if _table_exists(conn, "fb_meta_page_tokens"):
            cols = _columns(conn, "fb_meta_page_tokens")
            if "health_status" in cols:
                for t in conn.execute(f"""SELECT page_id, health_status, health_message, last_checked_at
                    FROM fb_meta_page_tokens WHERE page_id IN ({marks})""", ids):
                    tokens[t["page_id"]] = t
        token_messages = {
            "invalid": ("critical", "Token Facebook không còn hiệu lực"),
            "missing_permissions": ("critical", "Token Facebook thiếu quyền cần thiết"),
            "expiring": ("warning", "Token Facebook sắp hết hạn"),
            "check_failed": ("warning", "Không kiểm tra được token Facebook"),
        }
        for pid, page in page_by_id.items():
            t = tokens.get(pid)
            if t and t["health_status"] in token_messages:
                severity, title = token_messages[t["health_status"]]
                issues.append(_issue(page, "facebook", severity, title,
                                     t["health_message"] or "", "page", pid, t["last_checked_at"]))
            elif page["connection_status"] == "needs_reconnect":
                issues.append(_issue(page, "facebook", "critical", "Page cần kết nối lại với Meta",
                                     "Tự động đăng đã bị tắt cho Page này.", "page", pid, page["updated_at"]))

        # 4. Publishing problems.
        if _table_exists(conn, "fb_publish_schedule"):
            cols = _columns(conn, "fb_publish_schedule")
            error_col = "q.publish_error" if "publish_error" in cols else "NULL"
            publish_states = {
                "unknown": ("critical", "Chưa xác định bài đã đăng hay chưa",
                            "Kiểm tra trực tiếp trên Page trước khi xử lý để tránh đăng trùng."),
                "failed": ("warning", "Đăng bài thất bại", ""),
                "blocked": ("warning", "Bài bị chặn trước khi đăng", ""),
            }
            for q in conn.execute(f"""
              SELECT q.id, q.page_id, q.state, q.updated_at, {error_col} AS publish_error, d.title
              FROM fb_publish_schedule q JOIN fb_drafts d ON d.id=q.draft_id
              WHERE q.page_id IN ({marks}) AND q.state IN ('unknown','failed','blocked')
              ORDER BY q.updated_at DESC LIMIT 200""", ids):
                severity, title, hint = publish_states[q["state"]]
                detail = " ".join(part for part in (q["publish_error"] or "", hint) if part)
                issues.append(_issue(page_by_id[q["page_id"]], "publish", severity,
                                     f"{title}: {q['title']}", detail, "schedule", q["id"],
                                     q["updated_at"]))

    issues.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], i["page_name"], -_ts(i["at"])))
    return issues


@router.get("/issues")
def list_issues(page_id: str | None = None):
    issues = collect_issues(page_id)
    counts = {level: 0 for level in SEVERITY_ORDER}
    per_page: dict[str, dict] = {}
    for issue in issues:
        counts[issue["severity"]] += 1
        row = per_page.setdefault(issue["page_id"] or "", {
            "page_id": issue["page_id"], "page_name": issue["page_name"],
            "critical": 0, "warning": 0, "info": 0})
        row[issue["severity"]] += 1
    return {
        "counts": counts, "pages": list(per_page.values()), "items": issues,
        "notice": ("Cảnh báo Facebook hiện chỉ gồm tình trạng token/kết nối và lỗi đăng bài. "
                   "Chưa đọc được cảnh báo chính sách hay chất lượng Page từ Meta."),
    }
