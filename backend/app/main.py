from __future__ import annotations

import json
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, HttpUrl

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", str(APP_DIR.parent / "data" / "jobs.sqlite3")))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(APP_DIR.parent / "output"))).resolve()
API_KEY = os.getenv("API_KEY", "")
ALLOWED_ORIGINS = [
    item.strip()
    for item in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000").split(",")
    if item.strip()
]

app = FastAPI(
    title="AI Affiliate Studio API",
    version="0.1.0",
    description="Job API for producing short-form affiliate videos.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


def db_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    with db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                output_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


@app.on_event("startup")
def on_startup() -> None:
    initialize_database()


class CreateVideoJob(BaseModel):
    product_name: str = Field(min_length=2, max_length=160)
    product_benefits: str = Field(min_length=5, max_length=1200)
    problem: str = Field(min_length=5, max_length=800)
    call_to_action: str = Field(default="Xem sản phẩm ở giỏ hàng", max_length=180)
    duration_seconds: int = Field(default=40, ge=15, le=90)
    aspect_ratio: Literal["9:16"] = "9:16"
    product_url: HttpUrl | None = None


class JobAccepted(BaseModel):
    job_id: str
    status: Literal["queued"]
    message: str


class JobStatus(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    output_url: str | None = None
    error: str | None = None


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    # Never expose an unauthenticated job endpoint, even during early deployment.
    if not API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Backend chưa được cấu hình API_KEY.",
        )
    supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not secrets.compare_digest(supplied, API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key không hợp lệ.",
        )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai-affiliate-studio-api", "version": "0.1.0"}


@app.post("/v1/jobs", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
def create_job(payload: CreateVideoJob, _: None = Depends(require_api_key)) -> JobAccepted:
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO jobs (id, status, payload_json, created_at, updated_at)
            VALUES (?, 'queued', ?, ?, ?)
            """,
            (job_id, json.dumps(payload.model_dump(mode="json")), now, now),
        )
    return JobAccepted(
        job_id=job_id,
        status="queued",
        message="Đã nhận yêu cầu. Worker sẽ tạo video MP4 khi được khởi chạy.",
    )


@app.get("/v1/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str, _: None = Depends(require_api_key)) -> JobStatus:
    with db_connection() as connection:
        row = connection.execute(
            "SELECT id, status, output_path, error, created_at, updated_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ.")
    return JobStatus(
        job_id=row["id"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        output_url=f"/v1/jobs/{job_id}/download" if row["status"] == "completed" and row["output_path"] else None,
        error=row["error"],
    )


@app.get("/v1/jobs/{job_id}/download")
def download_job(job_id: str, _: None = Depends(require_api_key)) -> FileResponse:
    with db_connection() as connection:
        row = connection.execute(
            "SELECT status, output_path FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tác vụ.")
    if row["status"] != "completed" or not row["output_path"]:
        raise HTTPException(status_code=409, detail="Video chưa được tạo xong.")
    output_path = Path(row["output_path"]).resolve()
    if OUTPUT_DIR not in output_path.parents or not output_path.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp video.")
    return FileResponse(output_path, media_type="video/mp4", filename=f"{job_id}.mp4")
