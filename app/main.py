from __future__ import annotations

import logging
import os
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.jobs import JobSpec, jobs
from app.vixal_client import client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vixal_auto")

# How long /api/save?wait=1 blocks before handing back a still-running job.
WAIT_TIMEOUT = settings.process_timeout

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    jobs.start()
    try:
        await client.start()
    except Exception as exc:  # noqa: BLE001 - boot the web UI even if Telegram isn't ready
        client.start_error = str(exc)
        log.warning("Telegram session not ready: %s", exc)
    try:
        yield
    finally:
        await jobs.stop()
        await client.stop()


app = FastAPI(title="vixal_auto → copytele", lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _safe_stem(name: str) -> str:
    stem = os.path.splitext(os.path.basename(name or "vixal"))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return stem or "vixal"


def _kind_for(filename: str, content_type: str = "") -> str | None:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in VIDEO_EXTS or (content_type or "").startswith("video/"):
        return "video"
    if ext in IMAGE_EXTS or (content_type or "").startswith("image/"):
        return "image"
    return None


async def _save_uploads(files: list[UploadFile]) -> list[str]:
    """Stream uploaded files to a fresh per-request temp dir. Returns paths."""
    workdir = os.path.join(settings.download_dir, "uploads", uuid.uuid4().hex)
    os.makedirs(workdir, exist_ok=True)
    paths: list[str] = []
    for i, f in enumerate(files):
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(f.filename or f"file{i}"))
        dest = os.path.join(workdir, f"{i:02d}_{base}")
        with open(dest, "wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
        paths.append(dest)
    return paths


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"target": settings.upload_base, "ready": client.ready, "error": client.start_error or ""},
    )


@app.post("/api/process")
async def api_process(
    files: list[UploadFile] = File(...),
    kind: str = Form("image"),
    prompt: str = Form("Nude"),
    template: str = Form("1"),
    breast: str = Form("small"),
    duration: str = Form("5s"),
    ratio: str = Form("9:16"),
    resolution: str = Form("480p"),
):
    """Browser endpoint. Multipart: one or more files + the options form.

    `kind=image` with multiple files runs the batch flow; one file runs single.
    `kind=video` expects exactly one file.
    """
    if not files:
        return JSONResponse({"status": "error", "error": "No files uploaded"}, status_code=400)
    if kind == "video" and len(files) > 1:
        return JSONResponse({"status": "error", "error": "Video mode takes one file"}, status_code=400)

    paths = await _save_uploads(files)
    spec = JobSpec(
        kind="video" if kind == "video" else "image",
        inputs=paths,
        stem=_safe_stem(files[0].filename),
        prompt=prompt,
        template=template, breast=breast, duration=duration,
        ratio=ratio, resolution=resolution,
    )
    job = jobs.enqueue(spec)
    return JSONResponse(job.public(), status_code=202)


@app.post("/api/save")
async def api_save(
    file: UploadFile = File(...),
    prompt: str = Form("Nude"),
    template: str = Form("1"),
    breast: str = Form("small"),
    duration: str = Form("5s"),
    ratio: str = Form("9:16"),
    resolution: str = Form("480p"),
    wait: int = Form(0),
):
    """iOS-Shortcut endpoint: POST one image or video as multipart `file`.

    Kind is inferred from the file. Default returns instantly with a queued
    job; pass wait=1 to block until it finishes so the Shortcut notification
    shows the real result + copytele URL.
    """
    kind = _kind_for(file.filename or "", file.content_type or "")
    if kind is None:
        return JSONResponse(
            {"status": "error", "error": f"Unsupported file type: {file.filename}"},
            status_code=400,
        )
    paths = await _save_uploads([file])
    spec = JobSpec(
        kind=kind, inputs=paths, stem=_safe_stem(file.filename), prompt=prompt,
        template=template, breast=breast, duration=duration,
        ratio=ratio, resolution=resolution,
    )
    job = jobs.enqueue(spec)
    if wait:
        job = await jobs.wait_for(job.id, WAIT_TIMEOUT)
    code = 202 if job.status in ("queued", "running") else 200
    return JSONResponse(job.public(), status_code=code)


@app.get("/api/jobs/{job_id}")
async def api_job_status(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        return JSONResponse({"status": "error", "error": "Unknown job id"}, status_code=404)
    return JSONResponse(job.public())


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "telegram_ready": client.ready, "error": client.start_error or ""}
