"""Rule-based automatic approval and scheduling for Pages the owner opts in.

The owner curates trusted sources per Page and does not review each post.
A draft is approved automatically only when every rule below passes; any
doubt keeps it held, and a held draft simply expires with the news cycle.
No AI judgement can approve a draft: AI output is only ever a reason to hold.

Rules (all required):
  1. The Page has automation on and the global stop is not active.
  2. The draft came from collected RSS items (manual drafts never qualify).
  3. Every item comes from an enabled source assigned to this Page and marked
     "official" or "established" by the owner.
  4. Every item has a real summary (not just a headline) and an http(s) link.
  5. The newest item is younger than the Page's freshness limit.
  6. A fresh evidence check shows no unsupported, contradicted or
     needs_review claims.
  7. No sensitive-topic keyword appears in the draft or its source text.
  8. Not a duplicate of content already approved for the Page.
  9. If an AI writer is configured, its rewrite has finished.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .duplicate_guard import ensure_history_schema, find_duplicate, remember_approved
from .facebook_hub import connect, init_hub_db, log, now, require_hub_key

router = APIRouter(prefix="/api/facebook-hub", tags=["Facebook Hub automation"],
                   dependencies=[Depends(require_hub_key)])

TRUSTED_LEVELS = ("official", "established")
RISKY_STATUSES = ("unsupported", "contradicted", "needs_review")
MIN_SUMMARY_CHARS = 40
FAILURES_BEFORE_AUTO_STOP = 3

# Topics never published without a human: wrong or careless posts here carry
# legal or safety risk. Matching is whole-word and case-insensitive.
SENSITIVE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "chính trị": ("bầu cử", "biểu tình", "chủ quyền", "phản động", "đảng phái", "lật đổ"),
    "tôn giáo": ("tôn giáo", "giáo hội", "tín ngưỡng", "đạo phật", "công giáo", "hồi giáo"),
    "y tế/thuốc": ("thuốc", "chữa khỏi", "vắc xin", "vaccine", "dịch bệnh", "ung thư", "thực phẩm chức năng"),
    "thương vong": ("tử vong", "thiệt mạng", "thương vong", "tử nạn", "thi thể", "mất tích"),
    "tội phạm/pháp lý": ("bắt giữ", "khởi tố", "truy nã", "nghi phạm", "bị can", "bị cáo", "lừa đảo", "án mạng"),
    "tài chính đầu tư": ("chứng khoán", "tiền ảo", "tiền điện tử", "bitcoin", "đa cấp", "lợi nhuận cam kết"),
}


def _keyword_regex(word: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)", re.I)


def ensure_automation_schema() -> None:
    init_hub_db()
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS fb_page_automation (
          page_id TEXT PRIMARY KEY REFERENCES fb_pages(id) ON DELETE CASCADE,
          auto_approve INTEGER NOT NULL DEFAULT 0,
          max_age_hours INTEGER NOT NULL DEFAULT 24,
          extra_block_keywords TEXT NOT NULL DEFAULT '',
          stopped_reason TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fb_auto_decisions (
          draft_id TEXT PRIMARY KEY REFERENCES fb_drafts(id) ON DELETE CASCADE,
          page_id TEXT NOT NULL,
          decision TEXT NOT NULL,
          reasons TEXT NOT NULL,
          decided_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fb_auto_decisions_time ON fb_auto_decisions(decided_at);
        """)


def get_page_automation(conn, page_id: str) -> dict:
    row = conn.execute("SELECT * FROM fb_page_automation WHERE page_id=?", (page_id,)).fetchone()
    if row is None:
        return {"page_id": page_id, "auto_approve": False, "max_age_hours": 24,
                "extra_block_keywords": "", "stopped_reason": ""}
    result = dict(row)
    result["auto_approve"] = bool(result["auto_approve"])
    return result


def _block_keywords(extra: str) -> list[tuple[str, str]]:
    words = [(topic, word) for topic, items in SENSITIVE_KEYWORDS.items() for word in items]
    for word in re.split(r"[,\n]", extra or ""):
        word = word.strip()
        if word:
            words.append(("từ khóa riêng của Page", word))
    return words


def evaluate_draft(conn, draft, settings: dict) -> tuple[bool, list[str], list[str]]:
    """Return (approve, failed_reasons, passed_rules). Never writes."""
    from .public_site import public_body

    failed: list[str] = []
    passed: list[str] = []
    ai_configured = all(os.getenv(k, "").strip() for k in ("AI_API_BASE_URL", "AI_API_KEY", "AI_MODEL"))
    if ai_configured and (draft["writer_status"] if "writer_status" in draft.keys() else "") not in ("done",):
        return False, ["Đang chờ AI viết lại xong."], passed

    items = conn.execute("""
      SELECT i.title, i.summary, i.item_url, i.first_seen_at, s.name AS source_name,
             s.enabled AS source_enabled, s.trust_level, s.is_official,
             EXISTS (SELECT 1 FROM fb_page_sources ps
                     WHERE ps.page_id=? AND ps.source_id=s.id AND ps.enabled=1) AS assigned
      FROM fb_item_processing pr
      JOIN fb_source_items i ON i.id=pr.item_id
      JOIN fb_sources s ON s.id=i.source_id
      WHERE pr.draft_id=?""", (draft["page_id"], draft["id"])).fetchall()
    if not items:
        return False, ["Bản nháp không đến từ nguồn RSS đã thu thập (bài thủ công không được tự duyệt)."], passed

    untrusted = [r["source_name"] for r in items
                 if not (r["source_enabled"] and r["assigned"]
                         and (r["trust_level"] in TRUSTED_LEVELS or r["is_official"]))]
    if untrusted:
        failed.append("Có tin từ nguồn chưa được đánh dấu uy tín/chính thức cho Page: "
                      + ", ".join(sorted(set(untrusted))))
    else:
        passed.append("Mọi tin đến từ nguồn uy tín đã gán cho Page: "
                      + ", ".join(sorted({r["source_name"] for r in items})))

    thin = [r["title"] for r in items if len((r["summary"] or "").strip()) < MIN_SUMMARY_CHARS]
    if thin:
        failed.append("Nguồn chỉ có tiêu đề, thiếu trích yếu để hiểu rõ nội dung.")
    elif not all(re.match(r"^https?://", r["item_url"] or "") for r in items):
        failed.append("Thiếu liên kết bài gốc hợp lệ.")
    else:
        passed.append("Có trích yếu và liên kết bài gốc.")

    newest = max(datetime.fromisoformat(r["first_seen_at"]) for r in items)
    if datetime.now(timezone.utc) - newest > timedelta(hours=settings["max_age_hours"]):
        failed.append(f"Tin cũ hơn {settings['max_age_hours']} giờ.")
    else:
        passed.append("Tin còn mới.")

    risky = conn.execute(f"""
      SELECT status, COUNT(*) AS n FROM fb_evidence_claims
      WHERE draft_id=? AND status IN ({",".join("?" * len(RISKY_STATUSES))}) GROUP BY status""",
      (draft["id"], *RISKY_STATUSES)).fetchall()
    if risky:
        failed.append("Sổ bằng chứng có cảnh báo: "
                      + ", ".join(f"{r['status']}={r['n']}" for r in risky))
    else:
        passed.append("Sổ bằng chứng không có cảnh báo.")

    text = " ".join([draft["title"] or "", public_body(draft["body"] or "")]
                    + [f"{r['title']} {r['summary'] or ''}" for r in items])
    hits = sorted({f"{topic}: “{word}”" for topic, word in _block_keywords(settings["extra_block_keywords"])
                   if _keyword_regex(word).search(text)})
    if hits:
        failed.append("Chủ đề nhạy cảm, cần người kiểm tra: " + "; ".join(hits[:5]))
    else:
        passed.append("Không có từ khóa chủ đề nhạy cảm.")

    duplicate = find_duplicate(conn, draft["page_id"], draft["title"], draft["body"],
                               draft["source_url"], exclude_draft_id=draft["id"])
    if duplicate:
        failed.append(f"Trùng nội dung đã duyệt: {duplicate['title']}")
    else:
        passed.append("Không trùng bài đã duyệt.")
    return not failed, failed, passed


CANDIDATE_TITLE_PREFIX = "Thông tin cần kiểm chứng: "


def publishable_text(conn, draft) -> tuple[str, str]:
    """Clean title/body for publishing: drop system prefixes and internal notes."""
    from .public_site import public_body

    title = (draft["title"] or "").strip()
    if title.startswith(CANDIDATE_TITLE_PREFIX):
        title = title[len(CANDIDATE_TITLE_PREFIX):].strip()
    body = public_body(draft["body"] or "")
    return title or (draft["title"] or ""), body


def _record(conn, draft, decision: str, reasons: list[str]) -> None:
    conn.execute("""INSERT INTO fb_auto_decisions(draft_id,page_id,decision,reasons,decided_at)
      VALUES(?,?,?,?,?) ON CONFLICT(draft_id) DO UPDATE SET decision=excluded.decision,
      reasons=excluded.reasons, decided_at=excluded.decided_at""",
      (draft["id"], draft["page_id"], decision, json.dumps(reasons, ensure_ascii=False), now()))


def _auto_stop_if_failing(conn, page_id: str) -> str | None:
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fb_publish_schedule'").fetchone()
    if not table:
        return None
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    failures = conn.execute("""SELECT COUNT(*) FROM fb_publish_schedule
      WHERE page_id=? AND state IN ('failed','unknown') AND updated_at>=?""", (page_id, since)).fetchone()[0]
    if failures >= FAILURES_BEFORE_AUTO_STOP:
        reason = f"Tự dừng: {failures} lần đăng lỗi/không rõ kết quả trong 24 giờ."
        conn.execute("""UPDATE fb_page_automation SET auto_approve=0, stopped_reason=?, updated_at=?
          WHERE page_id=?""", (reason, now(), page_id))
        log(conn, "automation.auto_stopped", f"{page_id}: {reason}", "warning")
        return reason
    return None


def run_automation(limit_per_page: int = 30) -> dict:
    """Evaluate held/pending drafts of opted-in Pages, approve the clean ones, schedule them."""
    from .evidence_checker import check_draft
    from .facebook_scheduler import get_settings, init_scheduler_db, plan_page

    from .content_processor import ensure_processing_schema

    ensure_automation_schema()
    init_scheduler_db()
    ensure_processing_schema()
    summary = {"pages": 0, "approved": 0, "held": 0, "scheduled": 0, "stopped": []}
    with connect() as conn:
        pages = conn.execute("""SELECT p.id FROM fb_pages p JOIN fb_page_automation a ON a.page_id=p.id
          WHERE p.enabled=1 AND a.auto_approve=1""").fetchall()
    for page in pages:
        page_id = page["id"]
        with connect() as conn:
            if _auto_stop_if_failing(conn, page_id):
                summary["stopped"].append(page_id)
                continue
            settings = get_page_automation(conn, page_id)
            since = (datetime.now(timezone.utc) - timedelta(hours=settings["max_age_hours"])).isoformat()
            draft_ids = [r["id"] for r in conn.execute("""
              SELECT id FROM fb_drafts WHERE page_id=? AND review_status IN ('pending','held')
                AND created_at>=? ORDER BY created_at DESC LIMIT ?""",
              (page_id, since, limit_per_page))]
        summary["pages"] += 1
        approved_ids: set[str] = set()
        for draft_id in draft_ids:
            check_draft(draft_id)  # refresh the evidence ledger before deciding
            with connect() as conn:
                draft = conn.execute("SELECT * FROM fb_drafts WHERE id=?", (draft_id,)).fetchone()
                if draft is None or draft["review_status"] not in ("pending", "held"):
                    continue
                previous = conn.execute("SELECT decision FROM fb_auto_decisions WHERE draft_id=?",
                                        (draft_id,)).fetchone()
                if previous and previous["decision"] in ("approved", "revoked"):
                    # Auto-approved earlier and since pulled back by a person: never re-approve.
                    if previous["decision"] == "approved":
                        _record(conn, draft, "revoked", ["Người quản trị đã gỡ bài này; không tự duyệt lại."])
                    continue
                ok, failed, passed = evaluate_draft(conn, draft, settings)
                if not ok:
                    _record(conn, draft, "held", failed)
                    summary["held"] += 1
                    continue
                reason = "Tự duyệt theo luật: " + " ".join(passed)
                title, body = publishable_text(conn, draft)
                cur = conn.execute("""UPDATE fb_drafts SET review_status='approved', review_reason=?,
                  title=?, body=?, updated_at=? WHERE id=? AND review_status IN ('pending','held')""",
                  (reason[:1000], title, body, now(), draft_id))
                if not cur.rowcount:
                    continue
                ensure_history_schema(conn)
                remember_approved(conn, conn.execute("SELECT * FROM fb_drafts WHERE id=?",
                                                     (draft_id,)).fetchone(), now())
                _record(conn, draft, "approved", passed)
                log(conn, "draft.auto_approved", f"{draft_id}: {'; '.join(passed)[:400]}")
                approved_ids.add(draft_id)
                summary["approved"] += 1
        with connect() as conn:
            if get_settings(conn, page_id)["enabled"]:
                created = plan_page(conn, page_id, days=2, newest_first=True)
                if created:
                    log(conn, "schedule.auto_planned", f"{page_id}: {len(created)} item(s)")
                summary["scheduled"] += len(created)
    return summary


class AutomationSettings(BaseModel):
    auto_approve: bool = False
    max_age_hours: int = Field(default=24, ge=2, le=72)
    extra_block_keywords: str = Field(default="", max_length=2000)


@router.get("/pages/{page_id}/automation")
def read_automation(page_id: str):
    ensure_automation_schema()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_pages WHERE id=?", (page_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy Page.")
        result = get_page_automation(conn, page_id)
    result["sensitive_topics"] = {k: list(v) for k, v in SENSITIVE_KEYWORDS.items()}
    return result


@router.put("/pages/{page_id}/automation")
def write_automation(page_id: str, payload: AutomationSettings):
    ensure_automation_schema()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM fb_pages WHERE id=?", (page_id,)).fetchone():
            raise HTTPException(404, "Không tìm thấy Page.")
        conn.execute("""INSERT INTO fb_page_automation
          (page_id,auto_approve,max_age_hours,extra_block_keywords,stopped_reason,updated_at)
          VALUES(?,?,?,?,'',?) ON CONFLICT(page_id) DO UPDATE SET auto_approve=excluded.auto_approve,
          max_age_hours=excluded.max_age_hours, extra_block_keywords=excluded.extra_block_keywords,
          stopped_reason='', updated_at=excluded.updated_at""",
          (page_id, int(payload.auto_approve), payload.max_age_hours,
           payload.extra_block_keywords.strip(), now()))
        log(conn, "automation.settings_updated", f"{page_id}: auto_approve={payload.auto_approve}")
    return {"ok": True}


@router.post("/automation/stop")
def emergency_stop():
    """Turn off automatic approval and Facebook auto-publishing for every Page."""
    ensure_automation_schema()
    with connect() as conn:
        conn.execute("UPDATE fb_page_automation SET auto_approve=0, stopped_reason=?, updated_at=?",
                     ("Dừng khẩn cấp bởi người quản trị.", now()))
        pages = conn.execute("UPDATE fb_pages SET auto_publish=0, updated_at=? WHERE auto_publish=1",
                             (now(),)).rowcount
        log(conn, "automation.emergency_stop", f"auto_publish turned off on {pages} Page(s)", "warning")
    return {"ok": True, "pages_auto_publish_off": pages,
            "note": "Đã tắt tự duyệt và tự đăng. Bật lại từng Page khi đã kiểm tra."}


@router.get("/automation/decisions")
def list_decisions(hours: int = 48, decision: str | None = None):
    if hours < 1 or hours > 168:
        raise HTTPException(422, "hours phải từ 1 đến 168.")
    if decision not in (None, "approved", "held"):
        raise HTTPException(422, "decision phải là approved hoặc held.")
    from .facebook_scheduler import init_scheduler_db

    ensure_automation_schema()
    init_scheduler_db()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with connect() as conn:
        rows = conn.execute("""
          SELECT a.*, d.title, d.review_status, p.name AS page_name,
            (SELECT q.state FROM fb_publish_schedule q WHERE q.draft_id=a.draft_id) AS schedule_state,
            (SELECT q.planned_at FROM fb_publish_schedule q WHERE q.draft_id=a.draft_id) AS planned_at
          FROM fb_auto_decisions a JOIN fb_drafts d ON d.id=a.draft_id JOIN fb_pages p ON p.id=a.page_id
          WHERE a.decided_at>=? """ + ("AND a.decision=? " if decision else "") +
          "ORDER BY a.decided_at DESC LIMIT 300",
          (since, decision) if decision else (since,)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["reasons"] = json.loads(item["reasons"])
        items.append(item)
    return {"items": items}
