# 🪄 vixal_auto → copytele

A small webapp that **automates the Vixal-clone Telegram bot** for you. Upload
an image (or several) or an MP4, pick the prompt / settings, and it drives the
whole bot conversation — `/start` → menu → upload → prompt → wait for the render
— then pushes the finished media straight onto your **copytele** (copyparty)
volume. Use it from the browser or from an **iPhone Share-Sheet Shortcut**.

```
file(s) ──► FastAPI ──► Telethon (your user session) ──► @vixal_i_clone_bot ──► result ──► HTTP PUT ──► copytele
```

It talks to the bot through the **MTProto user API** (Telethon), i.e. as *you* —
the same clicks and uploads you'd do by hand, scripted. There is no bot token;
the bot has no automation API, so a user session is the only way in.

## How it works

The bot is menu-driven. `app/vixal_client.py` reproduces the flows from
`flows.txt`:

| Mode | Conversation |
|---|---|
| Image, single | `/start` → **image to image V2** → **Single** → *send image* → **\<prompt\>** → result |
| Image, batch | `/start` → **image to image V2** → **Batch** → *send N images* → **No More Image, Select Prompt** → **\<prompt\>** → N results |
| Video | `/start` → **video to video V2** → *send mp4* → settings (**template / breast / duration / ratio / resolution**) → **Confirm Upload** → result |

All three flows are **validated live** against `@vixal_i_clone_bot` (2026-06).
Prompt options: `Nude, Bikini, Rope Bondage, Fishnet, Missionary, Blowjob,
Facial, Custom Prompt`.

> 💎 **Costs (live-verified):** image = **2 Gems**. Video = **30 Gems** for
> 5s/480p and rises with duration (10s = 60, 15s = 90 … 30s = 180 Gems) — much
> more than the bot's older docs implied. The web UI shows the Gem cost per
> duration. Telethon needs `hachoir` (a dependency) to send the mp4 as a proper
> streamable video, otherwise the bot silently ignores a document-mode upload.

Every inline button is matched by **regex patterns** (not exact text) and every
bot message + its buttons are logged, so a small change in the bot's wording is
easy to retune in one place (top of `app/vixal_client.py`) and failures tell you
exactly which buttons *were* available.

Jobs run **one at a time** in a background worker (one Telegram session = one
serial menu state). The HTTP request returns immediately with a job id you poll.

## Setup

Requires Python 3.11+.

```bash
cd ~/projects/vixal_auto
cp .env.example .env        # fill in TELEGRAM_API_ID / API_HASH / PHONE + COPYTELE_*
uv venv .venv && uv pip install --python .venv -r pyproject.toml
```

### 1. One-time login (authorizes your account)

Login is two non-interactive steps (no stdin prompt — the code/2FA come from
env vars). **Open a chat with the bot once** in Telegram first so it resolves.

```bash
# a) ask Telegram to send a login code (arrives in your Telegram app):
.venv/bin/python -m scripts.login request

# b) sign in with that code (add VIXAL_LOGIN_PASSWORD=<2fa> if you have 2FA):
VIXAL_LOGIN_CODE=12345 .venv/bin/python -m scripts.login signin
```

This writes `vixal_auto.session` (a live login — keep it secret). Running
`python -m scripts.login` with no argument is the same as the `request` step.

### 2. Run

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8090
```

Open <http://localhost:8090>. If the banner says the session isn't ready, redo
step 1. Run a **single uvicorn worker** — the job queue is in-process.

## Configuration (`.env`)

| Env var | Default | Meaning |
|---|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | — | From <https://my.telegram.org>. |
| `TELEGRAM_PHONE` | — | Your number, e.g. `+8490…`. Used only at login. |
| `TELEGRAM_VIXAL_AI_BOT_USERNAME` | `vixal_i_clone_bot` | The bot to drive (no `@`). |
| `TELEGRAM_SESSION` | `vixal_auto.session` | Telethon session file. |
| `COPYTELE_UPLOAD_URL` | `http://10.1.1.99:11117/source/vixal/` | Destination folder. Results go in `photos/` & `videos/`. **Must end `/`.** |
| `COPYTELE_PW` | *(empty)* | copyparty password; empty = open volume. |
| `OVERWRITE` | `false` | Replace same-name file instead of auto-renaming. |
| `DOWNLOAD_DIR` | `/tmp/vixal_auto` | Temp buffer for up/downloads; cleaned per job. |
| `PROCESS_TIMEOUT` | `900` | Max seconds to wait for the bot to render a job. |
| `HOST` / `PORT` | `0.0.0.0` / `8090` | Server bind. |

> ⚠️ Use copytele's **direct LAN origin** for `COPYTELE_UPLOAD_URL`, not the
> public Cloudflare URL — Cloudflare 524-times-out / caps large media uploads.

## Tuning the bot flow

If a job errors with *"Couldn't find a button … Available buttons: [...]"*, the
bot's wording differs from the defaults. Edit the pattern lists at the top of
`app/vixal_client.py` (`BTN_IMG2IMG`, `BTN_SINGLE`, `BTN_SELECT_PROMPT`,
`BTN_CONFIRM`, …) to match — they're case-insensitive regexes against the visible
button text. The server log prints every received message and its buttons.

The **prompt** you type (e.g. `Nude`) is matched against the prompt-menu buttons,
so it must be one the bot offers. Video `count` / `quality` / `duration` are
clicked only if a button with that value exists, otherwise left at the default.

## API

| Method | Path | Body | Notes |
|---|---|---|---|
| `POST` | `/api/process` | multipart: `files[]`, `kind`, `prompt`/`count`/`quality`/`duration` | Web UI. Queues, returns job (202). |
| `POST` | `/api/save` | multipart: `file`, optional `prompt`/…/`wait` | iOS Shortcut. Kind inferred from the file. |
| `GET` | `/api/jobs/{id}` | — | Poll job status. |
| `GET` | `/healthz` | — | Health + `telegram_ready`. |

Finished job (`/api/jobs/{id}`):

```json
{ "id": "ab12…", "status": "done", "kind": "image", "count": 1,
  "folder": "photos", "copytele_urls": ["http://…/source/vixal/photos/photo_20260629_143012.jpg"],
  "copytele_url": "http://…/source/vixal/photos/photo_20260629_143012.jpg" }
```

On failure: `"status":"error"` with an `"error"` message. While running,
`"progress"` carries a live step (e.g. *"uploading image 2/3"*, *"processing…"*).

Saved files are named by save time: `photo_<YYYYmmdd_HHMMSS>` (in `photos/`) and
`clip_<YYYYmmdd_HHMMSS>` (in `videos/`). A batch shares one timestamp with a
`_N` suffix per image (`photo_20260629_143012_2.jpg`).

## 📱 iPhone Shortcut (share a photo/video → process → save)

1. **Shortcuts** app → **+** → name it e.g. *"Vixal it"*.
2. Settings (ⓘ) → enable **Show in Share Sheet**, set **Share Sheet Types** to
   **Images and Media** (and Files if you want).
3. Actions:
   - **Receive** *Images / Media* from *Share Sheet*.
   - **Get Contents of URL**:
     - URL: `https://vixal.yourdomain/api/save`
     - **Method: POST**
     - **Request Body: Form**
     - Add field `file` → type **File** → value = **Shortcut Input**.
     - *(optional, images)* add `prompt` = `Nude`.
     - *(optional, videos)* add `template`/`breast`/`duration`/`ratio`/`resolution`
       (defaults: `1`/`small`/`5s`/`9:16`/`480p` = cheapest, 30 Gems).
     - *(optional)* `wait` = `1` for a confirmed ✅/❌ result in the notification.
   - *(optional)* **Show Notification** with the result so you get a ✅/❌ + URL.
4. In Photos / any app: **Share → Vixal it**. Done.

**Instant vs. confirmed:** as written it returns immediately (job queued in the
background). Add the form field **`wait` = `1`** to make the request block until
the bot finishes (up to `PROCESS_TIMEOUT`) so the notification shows the real
✅/❌ and the copytele link.

## Docker

```bash
.venv/bin/python -m scripts.login          # create vixal_auto.session on the host first
docker compose up -d --build
```

`docker-compose.yml` mounts `./vixal_auto.session` into the container so it runs
headlessly, and reads secrets from `.env`. Point nginx-proxy-manager at the
container on port **8090**.

## CI → GHCR

`.github/workflows/docker-publish.yml` builds the image and pushes it to the
**GitHub Container Registry** on every push to `main`, on `v*` tags, and on
manual dispatch. No secrets to configure — it authenticates with the built-in
`GITHUB_TOKEN` (the workflow grants it `packages: write`).

Image: `ghcr.io/<owner>/vixal_auto`, tagged `latest` (on main), the short commit
`sha-xxxxxxx`, and `1.2`/`1.2.3` for `v*` tags.

First-time setup:

```bash
cd ~/projects/vixal_auto
git init && git add -A && git commit -m "Initial commit"
git branch -M main
git remote add origin git@github.com:<owner>/vixal_auto.git
git push -u origin main          # triggers the build
```

> The new package starts **private**. To pull it on your server either make it
> public (GitHub → Packages → vixal_auto → Package settings → visibility), or
> `docker login ghcr.io -u <owner> -p <a PAT with read:packages>` first.

Deploy on the server:

```bash
# in docker-compose.yml: comment out `build: .`, uncomment the `image:` line
docker compose pull && docker compose up -d
```

> ⚠️ `.gitignore` keeps `.env` and `*.session` out of git, and `.dockerignore`
> keeps them out of the image — the **session is mounted at runtime**, never
> baked in. Don't commit either.
