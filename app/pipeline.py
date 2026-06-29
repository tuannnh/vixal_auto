from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Callable

from app.uploader import upload_to_copytele
from app.vixal_client import client

StatusCb = Callable[[str], None]

# Job kinds.
IMAGE, VIDEO = "image", "video"


def _result_name(kind: str, ext: str, idx: int, total: int, ts: str) -> str:
    """Timestamped output name, e.g. photo_20260629_143012 / clip_20260629_143012.

    For a batch (total > 1) the files share the job timestamp and get a _N suffix.
    """
    ext = ext if ext.startswith(".") else f".{ext}"
    prefix = "clip" if kind == VIDEO else "photo"
    suffix = f"_{idx + 1}" if total > 1 else ""
    return f"{prefix}_{ts}{suffix}{ext}"


async def process(spec, status: StatusCb | None = None) -> dict:
    """Drive the bot for one job, push every output to copytele, clean up.

    `spec` is a JobSpec (see app.jobs). Returns a result dict for the Job.
    Raises on failure; the caller records the error.
    """
    say = status or (lambda _m: None)
    outputs: list[str] = []
    try:
        if spec.kind == VIDEO:
            outputs = await client.process_video(
                spec.inputs[0], spec.video_settings(), status=say
            )
            subfolder = "videos"
        else:
            outputs = await client.process_images(spec.inputs, spec.prompt, status=say)
            subfolder = "photos"

        urls: list[str] = []
        total = len(outputs)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")  # one stamp per job
        for i, path in enumerate(outputs):
            ext = os.path.splitext(path)[1] or (".mp4" if spec.kind == VIDEO else ".jpg")
            name = _result_name(spec.kind, ext, i, total, ts)
            say(f"uploading result {i + 1}/{total} to copytele")
            url = await asyncio.to_thread(upload_to_copytele, path, name, subfolder)
            urls.append(url)

        return {
            "kind": spec.kind,
            "count": total,
            "folder": subfolder,
            "copytele_urls": urls,
            "copytele_url": urls[0] if urls else "",
        }
    finally:
        # Never leave the originals or downloaded results on disk.
        for p in [*spec.inputs, *outputs]:
            try:
                os.remove(p)
            except OSError:
                pass
