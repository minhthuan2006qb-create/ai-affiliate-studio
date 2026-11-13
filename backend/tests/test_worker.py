import json
import sqlite3
from pathlib import Path

from app import worker


def create_job(db_path: Path, job_id: str = "job-test") -> None:
    worker.ensure_schema(db_path)
    with worker.connect_db(db_path) as connection:
        connection.execute(
            "INSERT INTO jobs (id, status, payload_json, created_at, updated_at) "
            "VALUES (?, 'queued', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
            (job_id, json.dumps({"product_name": "Kệ úp bát", "duration_seconds": 30})),
        )


def test_claim_next_job_changes_status_to_processing(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    create_job(db_path)
    job = worker.claim_next_job(db_path)
    assert job is not None
    assert job["id"] == "job-test"
    with worker.connect_db(db_path) as connection:
        row = connection.execute("SELECT status FROM jobs WHERE id = ?", ("job-test",)).fetchone()
    assert row["status"] == "processing"


def test_process_one_job_marks_completed_when_render_succeeds(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.sqlite3"
    create_job(db_path)
    output = tmp_path / "output"
    rendered = {}

    def fake_render(payload, destination, ffmpeg_bin="ffmpeg"):
        rendered["payload"] = payload
        rendered["destination"] = destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake-mp4")

    monkeypatch.setattr(worker, "render_video", fake_render)
    assert worker.process_one_job(db_path, output) is True
    with worker.connect_db(db_path) as connection:
        row = connection.execute("SELECT status, output_path, error FROM jobs WHERE id = ?", ("job-test",)).fetchone()
    assert row["status"] == "completed"
    assert Path(row["output_path"]).read_bytes() == b"fake-mp4"
    assert row["error"] is None


def test_process_one_job_records_render_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.sqlite3"
    create_job(db_path)

    def broken_render(payload, destination, ffmpeg_bin="ffmpeg"):
        raise RuntimeError("ffmpeg missing")

    monkeypatch.setattr(worker, "render_video", broken_render)
    assert worker.process_one_job(db_path, tmp_path / "output") is True
    with worker.connect_db(db_path) as connection:
        row = connection.execute("SELECT status, error FROM jobs WHERE id = ?", ("job-test",)).fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "ffmpeg missing"


def test_process_one_job_returns_false_when_queue_is_empty(tmp_path):
    db_path = tmp_path / "jobs.sqlite3"
    worker.ensure_schema(db_path)
    assert worker.process_one_job(db_path, tmp_path / "output") is False
