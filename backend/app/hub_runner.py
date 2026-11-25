"""Single long-running process for the whole Facebook Hub pipeline.

One process is easier to keep alive on a small server than six separate
workers. Each task runs on its own interval, failures are logged and never
stop the other tasks, and every task records a heartbeat so the Hub can show
when background work has stopped.

Publishing to Facebook still requires META_PUBLISHING_ENABLED=true; this
runner does not change that switch.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from typing import Callable

from .facebook_hub import connect, init_hub_db, now

HEARTBEAT_TABLE = """
CREATE TABLE IF NOT EXISTS fb_worker_heartbeats (
  task TEXT PRIMARY KEY,
  last_run_at TEXT NOT NULL,
  last_status TEXT NOT NULL,
  last_message TEXT NOT NULL DEFAULT '',
  interval_seconds INTEGER NOT NULL
)
"""


def ensure_heartbeat_schema() -> None:
    init_hub_db()
    with connect() as conn:
        conn.execute(HEARTBEAT_TABLE)


def record_heartbeat(task: str, status: str, message: str, interval: int) -> None:
    with connect() as conn:
        conn.execute(HEARTBEAT_TABLE)
        conn.execute("""INSERT INTO fb_worker_heartbeats(task,last_run_at,last_status,last_message,interval_seconds)
          VALUES(?,?,?,?,?) ON CONFLICT(task) DO UPDATE SET last_run_at=excluded.last_run_at,
          last_status=excluded.last_status, last_message=excluded.last_message,
          interval_seconds=excluded.interval_seconds""",
          (task, now(), status, message[:500], interval))


def _sources_cycle() -> str:
    from .source_worker import poll_all
    from .content_processor import process_unprocessed
    from .ai_writer import rewrite_pending
    from .evidence_checker import check_unchecked
    polled = poll_all()
    processed = process_unprocessed()
    rewritten = rewrite_pending()  # no-op when no AI provider is configured
    checked = check_unchecked()
    errors = sum(1 for r in polled if r.get("status") == "error")
    return (f"sources={len(polled)} errors={errors} new_items="
            f"{sum(int(r.get('new_items', 0)) for r in polled)} processed={len(processed)} "
            f"rewritten={sum(1 for r in rewritten if r.get('status') == 'done')} "
            f"evidence_checked={len(checked)}")


def _schedule_cycle() -> str:
    from .facebook_scheduler import init_scheduler_db, process_due_schedules
    init_scheduler_db()
    result = process_due_schedules(50)
    return f"checked={result['checked']} ready={result['ready']} blocked={result['blocked']}"


def _publish_cycle() -> str:
    from .facebook_publisher import PUBLISH_ENABLED, init_publisher_db, process_ready_items
    if not PUBLISH_ENABLED:
        return "disabled (META_PUBLISHING_ENABLED is not true)"
    init_publisher_db()
    result = process_ready_items(10)
    return json.dumps(result, ensure_ascii=False)[:400]


def _token_cycle() -> str:
    from .facebook_token_health import check_all_tokens, init_token_health_db
    if not os.getenv("META_TOKEN_ENCRYPTION_KEY", "").strip():
        return "skipped (Meta is not configured)"
    init_token_health_db()
    results = check_all_tokens()
    return f"pages={len(results)} " + ",".join(r["health_status"] for r in results)


def build_tasks(source_minutes: int) -> list[tuple[str, int, Callable[[], str]]]:
    return [
        ("sources", source_minutes * 60, _sources_cycle),
        ("schedule", 60, _schedule_cycle),
        ("publish", 30, _publish_cycle),
        ("token_health", 12 * 3600, _token_cycle),
    ]


def run_task(name: str, interval: int, func: Callable[[], str]) -> dict:
    try:
        message = func()
        status = "ok"
    except Exception as exc:  # keep the other tasks running
        message = f"{type(exc).__name__}: {exc}"[:500]
        status = "error"
        traceback.print_exc()
    try:
        record_heartbeat(name, status, message, interval)
    except Exception:
        traceback.print_exc()
    entry = {"task": name, "status": status, "message": message, "at": now()}
    print(json.dumps(entry, ensure_ascii=False), flush=True)
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Facebook Hub pipeline continuously")
    parser.add_argument("--once", action="store_true", help="Run every task once and exit")
    parser.add_argument("--source-interval-minutes", type=int,
                        default=int(os.getenv("HUB_SOURCE_INTERVAL_MINUTES", "30")))
    args = parser.parse_args()
    if args.source_interval_minutes < 5:
        parser.error("--source-interval-minutes không được nhỏ hơn 5 để tránh gây tải nguồn.")
    ensure_heartbeat_schema()
    tasks = build_tasks(args.source_interval_minutes)
    next_run = {name: 0.0 for name, _, _ in tasks}
    print(json.dumps({"runner": "started", "tasks": [t[0] for t in tasks]}), flush=True)
    while True:
        for name, interval, func in tasks:
            if time.monotonic() >= next_run[name]:
                run_task(name, interval, func)
                next_run[name] = time.monotonic() + interval
        if args.once:
            break
        time.sleep(5)


if __name__ == "__main__":
    main()
