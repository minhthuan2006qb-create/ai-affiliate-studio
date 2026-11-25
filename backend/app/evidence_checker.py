from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from collections import defaultdict

from .facebook_hub import connect, init_hub_db, log, now


# Deliberately narrow extraction: only explicit dates, times and numeric facts.
# This is metadata-assisted triage, not natural-language fact verification.
PATTERNS = {
    "date": re.compile(
        r"(?<!\d)(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|"
        r"\d{1,2}\s+tháng\s+\d{1,2}(?:\s+năm\s+\d{4})?)(?!\d)",
        re.I,
    ),
    "time": re.compile(r"(?<!\d)(\d{1,2}\s*(?:giờ|h)(?:\s*\d{1,2}\s*(?:phút|p)?)?)(?!\w)", re.I),
    "metric": re.compile(
        r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(%|triệu|tỷ|người|km|giờ|ngày|độ|đồng|lần|ca)\b",
        re.I,
    ),
}


def ensure_evidence_schema() -> None:
    init_hub_db()
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS fb_evidence_claims (
          id TEXT PRIMARY KEY,
          draft_id TEXT NOT NULL REFERENCES fb_drafts(id) ON DELETE CASCADE,
          claim_type TEXT NOT NULL,
          claim_text TEXT NOT NULL,
          normalized_value TEXT NOT NULL,
          status TEXT NOT NULL,
          supporting_groups INTEGER NOT NULL DEFAULT 0,
          source_count INTEGER NOT NULL DEFAULT 0,
          rationale TEXT NOT NULL,
          checked_at TEXT NOT NULL,
          UNIQUE(draft_id, claim_type, normalized_value)
        );
        CREATE TABLE IF NOT EXISTS fb_evidence_records (
          id TEXT PRIMARY KEY,
          claim_id TEXT NOT NULL REFERENCES fb_evidence_claims(id) ON DELETE CASCADE,
          source_item_id TEXT NOT NULL REFERENCES fb_source_items(id) ON DELETE CASCADE,
          source_id TEXT NOT NULL REFERENCES fb_sources(id) ON DELETE CASCADE,
          publisher_group TEXT NOT NULL DEFAULT '',
          source_url TEXT NOT NULL,
          excerpt TEXT NOT NULL,
          observed_value TEXT NOT NULL,
          trusted_group INTEGER NOT NULL DEFAULT 0,
          collected_at TEXT NOT NULL,
          UNIQUE(claim_id, source_item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_fb_evidence_draft
          ON fb_evidence_claims(draft_id, status);
        """)


def extract_claims(title: str, summary: str | None) -> list[tuple[str, str, str]]:
    text = " ".join(part for part in (title or "", summary or "") if part)
    found: dict[tuple[str, str], str] = {}
    for claim_type in ("date", "time"):
        for match in PATTERNS[claim_type].finditer(text):
            value = re.sub(r"\s+", " ", match.group(1)).strip()
            found[(claim_type, value.casefold())] = value
    for match in PATTERNS["metric"].finditer(text):
        value = re.sub(r"\s+", " ", match.group(0)).strip()
        unit = match.group(2).casefold()
        normalized = f"{match.group(1).replace(',', '.')} {unit}"
        found[(f"metric:{unit}", normalized)] = value
    return [(kind, value, normalized) for (kind, normalized), value in found.items()]



# Conservative semantic-risk cues. These patterns do not prove truth; they only
# flag explicit quotations, named speakers and named places for human review.
# Uppercase letters only (ASCII + Vietnamese). Name parts must start with one of
# these; no global re.I, otherwise verbs after a name ("thông báo") or common
# words after a preposition ("đến đây") would be captured as names.
_UPPER = "A-ZÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂĐĨŨƠƯẠẢẤẦẨẪẬẮẰẲẴẶẸẺẼẾỀỂỄỆỈỊỌỎỐỒỔỖỘỚỜỞỠỢỤỦỨỪỬỮỰỲỴỶỸ"
# Words of a name are separated by spaces/tabs only, never by a line break, so a
# title and the first word of the body cannot merge into one name.
_NAME = rf"([{_UPPER}]\w*(?:[ \t]+[{_UPPER}]\w*){{0,4}})"
PERSON_PATTERN = re.compile(
    r"\b(?i:ông|bà|anh|chị|th\.?s|tiến sĩ|bác sĩ|chủ tịch|giám đốc|"
    r"thủ tướng|bộ trưởng|bí thư)[ \t]+" + _NAME
)
PLACE_PATTERN = re.compile(
    r"\b(?i:tại|ở|đến|thuộc|trên địa bàn)[ \t]+" + _NAME
)
QUOTE_PATTERN = re.compile(r"[“\"']([^”\"']{12,240})[”\"']")


def extract_semantic_risk_claims(text: str) -> list[tuple[str, str, str]]:
    found: dict[tuple[str, str], str] = {}
    for kind, pattern in (("person", PERSON_PATTERN), ("place", PLACE_PATTERN),
                          ("quote", QUOTE_PATTERN)):
        for match in pattern.finditer(text or ""):
            value = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;")
            if len(value) < 3:
                continue
            normalized = value.casefold()
            found[(kind, normalized)] = value
    return [(kind, value, normalized)
            for (kind, normalized), value in found.items()]

def _trusted_group(row: sqlite3.Row) -> str:
    group = (row["publisher_group"] or "").strip().casefold()
    if group and (row["trust_level"] in {"official", "established"} or row["is_official"]):
        return group
    return ""


def check_draft(draft_id: str) -> dict[str, object]:
    ensure_evidence_schema()
    with connect() as conn:
        draft = conn.execute("SELECT id, title, body, review_status FROM fb_drafts WHERE id=?", (draft_id,)).fetchone()
        if draft is None:
            return {"draft_id": draft_id, "status": "missing", "claims": 0}
        # Rebuild evidence on each pass; cascade removes stale evidence records.
        conn.execute("DELETE FROM fb_evidence_claims WHERE draft_id=?", (draft_id,))
        rows = conn.execute("""
          SELECT DISTINCT i.id AS item_id, i.title, i.summary, i.item_url,
                 s.id AS source_id, s.name AS source_name, s.trust_level,
                 s.publisher_group, s.is_official
          FROM fb_item_processing p
          JOIN fb_source_items i ON i.id=p.item_id
          JOIN fb_sources s ON s.id=i.source_id
          WHERE p.draft_id=?
          ORDER BY i.first_seen_at, i.id
        """, (draft_id,)).fetchall()

        observations: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
        for row in rows:
            for claim_type, value, normalized in extract_claims(row["title"], row["summary"]):
                observations[(claim_type, normalized)].append({
                    "row": row, "value": value, "group": _trusted_group(row),
                })

        # A conflict is only recorded when two different values of the same
        # narrowly-defined field are each independently repeated by >=2 trusted
        # publisher groups. A single disagreement is not enough to infer conflict.
        trusted_groups_by_type: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for (claim_type, normalized), items in observations.items():
            for item in items:
                if item["group"]:
                    trusted_groups_by_type[claim_type][normalized].add(str(item["group"]))

        contradictory_values = {
            (claim_type, value)
            for claim_type, values in trusted_groups_by_type.items()
            if sum(1 for groups in values.values() if len(groups) >= 2) >= 2
            for value, groups in values.items()
            if len(groups) >= 2
        }

        checked_at = now()
        summaries = []
        for (claim_type, normalized), items in observations.items():
            trusted_groups = {str(item["group"]) for item in items if item["group"]}
            if (claim_type, normalized) in contradictory_values:
                status = "contradicted"
                rationale = "Có giá trị khác cùng loại dữ kiện; mỗi giá trị đều được lặp lại bởi ít nhất hai nhóm nhà xuất bản được khai báo đáng tin cậy."
            elif len(trusted_groups) >= 2:
                status = "supported"
                rationale = "Cùng dữ kiện xuất hiện ở ít nhất hai nhóm nhà xuất bản được khai báo độc lập; vẫn cần kiểm tra nội dung bài gốc."
            elif len(items) >= 2:
                status = "unverified"
                rationale = "Có nhiều bản ghi nhưng chưa đủ hai nhóm nhà xuất bản độc lập được khai báo và xác thực."
            else:
                status = "insufficient"
                rationale = "Chỉ tìm thấy một bản ghi cho dữ kiện; chưa đủ bằng chứng đối chiếu."

            claim_id = hashlib.sha256(
                f"claim:{draft_id}:{claim_type}:{normalized}".encode()
            ).hexdigest()
            conn.execute("""
              INSERT INTO fb_evidence_claims
                (id,draft_id,claim_type,claim_text,normalized_value,status,
                 supporting_groups,source_count,rationale,checked_at)
              VALUES(?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(draft_id,claim_type,normalized_value) DO UPDATE SET
                claim_text=excluded.claim_text,status=excluded.status,
                supporting_groups=excluded.supporting_groups,source_count=excluded.source_count,
                rationale=excluded.rationale,checked_at=excluded.checked_at
            """, (claim_id, draft_id, claim_type, items[0]["value"], normalized, status,
                  len(trusted_groups), len(items), rationale, checked_at))
            actual_claim = conn.execute("""
              SELECT id FROM fb_evidence_claims
              WHERE draft_id=? AND claim_type=? AND normalized_value=?
            """, (draft_id, claim_type, normalized)).fetchone()
            claim_id = actual_claim["id"]
            conn.execute("DELETE FROM fb_evidence_records WHERE claim_id=?", (claim_id,))
            for item in items:
                row = item["row"]
                excerpt = " ".join(part for part in (row["title"], row["summary"] or "") if part)[:1600]
                record_id = hashlib.sha256(f"evidence:{claim_id}:{row['item_id']}".encode()).hexdigest()
                conn.execute("""
                  INSERT INTO fb_evidence_records
                    (id,claim_id,source_item_id,source_id,publisher_group,source_url,
                     excerpt,observed_value,trusted_group,collected_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?)
                """, (record_id, claim_id, row["item_id"], row["source_id"],
                      item["group"], row["item_url"], excerpt, item["value"],
                      int(bool(item["group"])), checked_at))
            summaries.append({"claim_type": claim_type, "value": items[0]["value"],
                              "status": status, "supporting_groups": len(trusted_groups),
                              "source_count": len(items)})

        # Compare explicit names, place cues and quotations in the AI draft
        # with collected RSS text. Absence is a review signal, not proof of fabrication.
        source_text = " ".join(
            " ".join(part for part in (row["title"], row["summary"] or "") if part)
            for row in rows
        ).casefold()
        for claim_type, value, normalized in extract_semantic_risk_claims(
            "\n".join((draft["title"] or "", draft["body"] or ""))
        ):
            if normalized in source_text:
                continue
            claim_id = hashlib.sha256(
                ("claim:" + draft_id + ":" + claim_type + ":" + normalized).encode()
            ).hexdigest()
            rationale = (
                "Tên riêng/trích dẫn xuất hiện trong bản viết nhưng không tìm thấy nguyên văn "
                "trong dữ liệu RSS đã thu thập. Đây là tín hiệu cần kiểm tra bài gốc; "
                "không phải kết luận nội dung sai."
            )
            conn.execute(
                "INSERT INTO fb_evidence_claims "
                "(id,draft_id,claim_type,claim_text,normalized_value,status,"
                "supporting_groups,source_count,rationale,checked_at) "
                "VALUES(?,?,?,?,?,'unsupported',0,0,?,?)",
                (claim_id, draft_id, claim_type, value, normalized, rationale, checked_at),
            )
            summaries.append({"claim_type": claim_type, "value": value,
                              "status": "unsupported", "supporting_groups": 0,
                              "source_count": 0})

        # Compare explicit factual tokens in the AI draft with source metadata.
        for claim_type, value, normalized in extract_claims(draft["title"], draft["body"]):
            if (claim_type, normalized) in observations:
                continue
            claim_id = hashlib.sha256(
                ("claim:" + draft_id + ":" + claim_type + ":" + normalized).encode()
            ).hexdigest()
            rationale = (
                "Dữ kiện xuất hiện trong bản viết nhưng không có trong tiêu đề/trích yếu "
                "RSS đã thu thập. Có thể là chi tiết AI tự thêm; cần kiểm tra nguồn gốc."
            )
            conn.execute(
                "INSERT INTO fb_evidence_claims "
                "(id,draft_id,claim_type,claim_text,normalized_value,status,"
                "supporting_groups,source_count,rationale,checked_at) "
                "VALUES(?,?,?,?,?,'unsupported',0,0,?,?)",
                (claim_id, draft_id, claim_type, value, normalized, rationale, checked_at),
            )
            summaries.append({"claim_type": claim_type, "value": value,
                              "status": "unsupported", "supporting_groups": 0,
                              "source_count": 0})

        # The checker records evidence but never approves a draft. RSS excerpts
        # cannot prove that every claim in an AI-rewritten body is accurate.
        status_counts = {status: sum(1 for item in summaries if item["status"] == status)
                         for status in ("supported", "contradicted", "insufficient", "unverified", "unsupported")}
        overall = "insufficient" if not summaries else (
            "contradicted" if status_counts["contradicted"] else
            "unsupported" if status_counts["unsupported"] else
            "supported" if status_counts["supported"] == len(summaries) else "insufficient"
        )
        reason = (
            f"Evidence Checker: {len(summaries)} dữ kiện; "
            f"supported={status_counts['supported']}, contradicted={status_counts['contradicted']}, "
            f"insufficient={status_counts['insufficient']}, unverified={status_counts['unverified']}, "
            f"unsupported={status_counts['unsupported']}. "
            "Đã lưu sổ bằng chứng; chưa xác minh toàn văn nguồn gốc và chưa được tự động xuất bản."
        )
        conn.execute("""
          UPDATE fb_drafts SET review_status='held', review_reason=?, updated_at=?
          WHERE id=? AND review_status NOT IN ('approved','rejected','published')
        """, (reason, checked_at, draft_id))
        log(conn, "draft.evidence_checked", f"{draft_id}: {overall}; claims={len(summaries)}")
        return {"draft_id": draft_id, "status": overall, "claims": len(summaries),
                "counts": status_counts, "items": summaries}


def check_unchecked(limit: int = 50) -> list[dict[str, object]]:
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    ensure_evidence_schema()
    with connect() as conn:
        rows = conn.execute("""
          SELECT DISTINCT p.draft_id FROM fb_item_processing p
          JOIN fb_drafts d ON d.id=p.draft_id
          WHERE p.draft_id IS NOT NULL
            AND d.review_status NOT IN ('approved','rejected','published')
          ORDER BY d.created_at LIMIT ?
        """, (limit,)).fetchall()
    return [check_draft(row["draft_id"]) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Facebook Hub evidence ledger worker")
    parser.add_argument("--draft-id")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    results = [check_draft(args.draft_id)] if args.draft_id else check_unchecked(args.limit)
    print({"checked": len(results), "results": results}, flush=True)


if __name__ == "__main__":
    main()
