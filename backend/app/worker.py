from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = APP_DIR.parent / "data" / "jobs.sqlite3"
DEFAULT_OUTPUT_DIR = APP_DIR.parent / "output"
WIDTH, HEIGHT, FPS = 720, 1280, 24


def database_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", str(DEFAULT_DB_PATH)))


def output_dir() -> Path:
    return Path(os.getenv("OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_schema(path: Path) -> None:
    with connect_db(path) as connection:
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


def claim_next_job(path: Path) -> dict[str, Any] | None:
    ensure_schema(path)
    with connect_db(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id, payload_json FROM jobs WHERE status = 'queued' "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        connection.execute(
            "UPDATE jobs SET status = 'processing', updated_at = ? WHERE id = ? AND status = 'queued'",
            (now, row["id"]),
        )
        connection.commit()
        return {"id": row["id"], "payload": json.loads(row["payload_json"])}


def set_job_result(
    path: Path,
    job_id: str,
    status: str,
    output_path: str | None = None,
    error: str | None = None,
) -> None:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with connect_db(path) as connection:
        connection.execute(
            "UPDATE jobs SET status = ?, output_path = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, output_path, error, now, job_id),
        )


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = str(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def draw_scene(path: Path, eyebrow: str, heading: str, body: str, footer: str, scene_number: int) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), (15, 23, 42))
    pixels = image.load()
    for y in range(HEIGHT):
        ratio = y / HEIGHT
        color = (
            int(17 + ratio * 16),
            int(24 + ratio * 20),
            int(39 + ratio * 35),
        )
        for x in range(WIDTH):
            pixels[x, y] = color

    draw = ImageDraw.Draw(image)
    draw.ellipse((470, -170, 850, 210), fill=(30, 64, 100))
    draw.ellipse((-180, 900, 180, 1260), fill=(24, 54, 80))
    draw.rounded_rectangle((48, 250, 672, 1050), radius=36, fill=(248, 250, 252))
    eyebrow_font = load_font(25, True)
    heading_font = load_font(51, True)
    body_font = load_font(31)
    footer_font = load_font(26, True)
    dark = (15, 23, 42)
    muted = (71, 85, 105)

    draw.rounded_rectangle((82, 295, 330, 350), radius=22, fill=(219, 234, 254))
    draw.text((103, 307), eyebrow.upper(), font=eyebrow_font, fill=(29, 78, 216))

    y = 420
    for line in wrap_text(draw, heading, heading_font, 520)[:4]:
        draw.text((88, y), line, font=heading_font, fill=dark)
        y += 72
    y += 24
    for line in wrap_text(draw, body, body_font, 520)[:7]:
        draw.text((90, y), line, font=body_font, fill=muted)
        y += 48

    draw.rounded_rectangle((82, 930, 638, 1000), radius=24, fill=(30, 64, 175))
    draw.text((108, 950), footer[:48], font=footer_font, fill=(255, 255, 255))
    draw.text((52, 1125), "AI AFFILIATE STUDIO", font=load_font(22, True), fill=(203, 213, 225))
    draw.text((625, 1125), f"0{scene_number}", font=load_font(22, True), fill=(203, 213, 225))
    image.save(path, format="PNG", optimize=True)


def render_video(payload: dict[str, Any], destination: Path, ffmpeg_bin: str = "ffmpeg") -> None:
    duration = int(payload.get("duration_seconds", 40))
    if not 15 <= duration <= 90:
        raise ValueError("duration_seconds phải nằm trong khoảng 15–90.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    work_dir = destination.parent / f".render-{destination.stem}"
    work_dir.mkdir(parents=True, exist_ok=True)
    product = str(payload.get("product_name", "Sản phẩm tiện ích")).strip()
    problem = str(payload.get("problem", "Bạn đang gặp bất tiện trong sinh hoạt?")).strip()
    benefits = str(payload.get("product_benefits", "Thiết kế gọn gàng, dễ sử dụng.")).strip()
    cta = str(payload.get("call_to_action", "Xem sản phẩm ở giỏ hàng")).strip()

    scenes = [
        ("VẤN ĐỀ THƯỜNG GẶP", problem, "Đừng để bất tiện nhỏ làm mất thời gian mỗi ngày.", "CÙNG TÌM CÁCH GIẢI QUYẾT", 1),
        ("GIẢI PHÁP GỢI Ý", product, benefits, "GỌN HƠN • TIỆN HƠN • DỄ DÙNG", 2),
        ("XEM THỬ SẢN PHẨM", product, "Tìm hiểu thông tin và chọn sản phẩm phù hợp với nhu cầu của bạn.", cta, 3),
    ]
    scene_paths: list[Path] = []
    for index, scene in enumerate(scenes):
        scene_path = work_dir / f"scene-{index + 1}.png"
        draw_scene(scene_path, *scene)
        scene_paths.append(scene_path)

    base = duration // 3
    durations = [base, base, duration - base * 2]
    inputs: list[str] = []
    filters: list[str] = []
    for index, (scene_path, seconds) in enumerate(zip(scene_paths, durations)):
        inputs.extend(["-loop", "1", "-framerate", str(FPS), "-t", str(seconds), "-i", str(scene_path)])
        frames = seconds * FPS
        filters.append(
            f"[{index}:v]scale={WIDTH}:{HEIGHT},"
            f"zoompan=z='min(zoom+0.0005,1.035)':d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},"
            f"setsar=1[v{index}]"
        )
    concat_inputs = "".join(f"[v{i}]" for i in range(len(scene_paths)))
    filters.append(f"{concat_inputs}concat=n=3:v=1:a=0[outv]")
    command = [
        ffmpeg_bin, "-y", *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[outv]", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-t", str(duration), str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as exc:
        raise RuntimeError("Không tìm thấy FFmpeg. Hãy cài FFmpeg hoặc dùng Docker image của backend.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or str(exc))[-1200:]
        raise RuntimeError(f"FFmpeg render thất bại: {detail}") from exc
    finally:
        for path in scene_paths:
            path.unlink(missing_ok=True)
        work_dir.rmdir()


def process_one_job(
    db_path: Path | None = None,
    out_dir: Path | None = None,
    ffmpeg_bin: str = "ffmpeg",
) -> bool:
    db_path = db_path or database_path()
    out_dir = out_dir or output_dir()
    job = claim_next_job(db_path)
    if job is None:
        return False
    destination = out_dir / f"{job['id']}.mp4"
    try:
        render_video(job["payload"], destination, ffmpeg_bin=ffmpeg_bin)
        set_job_result(db_path, job["id"], "completed", output_path=str(destination))
    except Exception as exc:
        set_job_result(db_path, job["id"], "failed", error=str(exc)[:2000])
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Affiliate Studio MP4 render worker")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit")
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    args = parser.parse_args()
    db_path = database_path()
    ensure_schema(db_path)
    print(f"Worker started. Database: {db_path}; output: {output_dir()}", flush=True)
    if args.once:
        process_one_job(db_path)
        return
    while True:
        if not process_one_job(db_path):
            time.sleep(max(0.5, args.poll_seconds))


if __name__ == "__main__":
    main()
