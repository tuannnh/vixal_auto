from __future__ import annotations

from urllib.parse import quote

import httpx

from app.config import settings


def upload_to_copytele(local_path: str, filename: str, subfolder: str = "") -> str:
    """PUT `local_path` onto the copytele/copyparty volume.

    copyparty stores a plain HTTP PUT at the URL path; intermediate folders
    (image/, video/) are created automatically. Returns the resulting file URL.
    Mirrors the music_downloader uploader.
    """
    prefix = f"{quote(subfolder)}/" if subfolder else ""
    target = settings.upload_base + prefix + quote(filename)
    params = {}
    if settings.copytele_pw:
        params["pw"] = settings.copytele_pw
    if settings.overwrite:
        params["replace"] = "1"

    with open(local_path, "rb") as f:
        resp = httpx.put(target, params=params, content=f, timeout=600.0)

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"copytele upload failed: HTTP {resp.status_code} — {resp.text[:300]}"
        )
    return target
