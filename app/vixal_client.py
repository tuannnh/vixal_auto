"""Drive the vixalAI-clone Telegram bot as a *user* (MTProto / Telethon).

The bot is menu-driven: you send `/start`, then click inline buttons, upload
media, pick a prompt / settings, and it streams back the processed media. We
reproduce that conversation programmatically.

Because we can't see the live bot from here, every button is matched by a list
of **regex patterns** (case-insensitive, against the button's visible text)
rather than an exact string — so small wording changes in the bot don't break
the flow, and you can tune the patterns below in one place. Every bot message
and its buttons are logged, so when a step can't find its button the error
lists exactly what *was* on offer.

Conversation shapes (see flows.txt):

  image→image, single : /start → [image to image V2] → [Single] → <send image>
                        → [<prompt>] → …queue… → processed image
  image→image, batch  : /start → [image to image V2] → [Batch] → <send N images>
                        → [Select Prompt] → [<prompt>] → …queue… → N processed images
  video→video         : /start → [video to video V2] → <send mp4>
                        → [<count>][<quality>][<duration>][confirm] → …queue… → processed video
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from PIL import Image
from telethon import TelegramClient, events
from telethon.tl.custom import Message

from app.config import settings

# Let Pillow open iPhone HEIC/HEIF photos (best-effort; harmless if unavailable).
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # noqa: BLE001
    pass

log = logging.getLogger("vixal_auto.bot")

StatusCb = Callable[[str], None]

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".gif"}

# Telegram photo limits: ≤ 10 MB and width+height ≤ 10000. We cap the longest
# side well under that and re-encode to JPEG so big phone photos are accepted.
PHOTO_MAX_DIM = 2560
PHOTO_MAX_BYTES = 9_500_000


def _prepare_photo(path: str) -> str:
    """Return a path Telegram will accept as a photo.

    If the image is already small enough (JPEG/PNG, within the size/dimension
    limits) the original path is returned unchanged. Otherwise it's downscaled
    and re-encoded to a JPEG in download_dir; that temp path is returned (and
    the caller deletes it after sending). Best-effort: on any error the original
    path is returned so the send can still be attempted.
    """
    try:
        size = os.path.getsize(path)
        with Image.open(path) as im:
            w, h = im.size
            fmt = (im.format or "").upper()
            ok = (size <= PHOTO_MAX_BYTES and (w + h) <= 10000
                  and max(w, h) <= PHOTO_MAX_DIM and fmt in ("JPEG", "PNG"))
            if ok:
                return path
            im = im.convert("RGB")
            scale = min(1.0, PHOTO_MAX_DIM / max(w, h))
            if scale < 1.0:
                im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                               Image.LANCZOS)
            os.makedirs(settings.download_dir, exist_ok=True)
            out = os.path.join(settings.download_dir, f"_tg_{uuid.uuid4().hex}.jpg")
            quality = 90
            im.save(out, "JPEG", quality=quality)
            while os.path.getsize(out) > PHOTO_MAX_BYTES and quality > 50:
                quality -= 10
                im.save(out, "JPEG", quality=quality)
            log.info("[prep] %s (%dx%d, %d bytes) -> %s (q=%d, %d bytes)",
                     os.path.basename(path), w, h, size, os.path.basename(out),
                     quality, os.path.getsize(out))
            return out
    except Exception as exc:  # noqa: BLE001
        log.warning("photo prep failed for %s (%s); sending original", path, exc)
        return path

# --------------------------------------------------------------------------- #
# Button / state patterns. Tune these to match your bot's exact wording.       #
#                                                                              #
# Validated live (2026-06) against @vixal_i_clone_bot:                         #
#   main menu : Image to Image V1/V2, Image to Video, Video to Video V1/V2,    #
#               Buy Gems, Claim Daily, Support   (so V2 must be required)      #
#   i2i modes : Single | Combine | Batch                                       #
#   single    : "Single Image Mode\nPlease send your image."                   #
#   batch     : "Batch Mode\nPlease send all your images (max 10)."; each      #
#               upload -> "Image added. Total: N" + one combined button        #
#               "No More Image, Select Prompt"                                 #
#   prompts   : Nude, Bikini, Rope Bondage, Fishnet, Missionary, Blowjob,      #
#               Facial, Custom Prompt   ("Select Prompt Option:")              #
#   video     : "Video to Video Mode\nPlease send an MP4 video to start."       #
#               then a "V2 Preview" + "Video to Video Settings" menu with        #
#               toggle groups (active = "[x]" prefix):                           #
#                 Template 1-5 | Breast small/medium/large |                     #
#                 Duration 5s..30s (Gem cost rises) | Ratio 9:16,16:9 |          #
#                 Resolution 480p,720p | "Confirm Upload (N Gems)"               #
# Costs (live): image=2 Gems, video=30 Gems @5s/480p (up to 180 @30s).          #
# Sending the mp4 needs proper video attributes (hachoir + supports_streaming)  #
# or the bot ignores it as a document. Whole flow validated end-to-end 2026-06. #
# --------------------------------------------------------------------------- #
# The main menu offers BOTH V1 and V2 of each mode — flows.txt uses V2, so the
# pattern must require V2 (a loose "image to image" would grab V1 first).
BTN_IMG2IMG = [r"image\s*to\s*image\s*v\s*2", r"\bi2i\b.*v\s*2"]
BTN_VID2VID = [r"video\s*to\s*video\s*v\s*2", r"\bv2v\b.*v\s*2"]
BTN_SINGLE = [r"\bsingle\b"]
BTN_BATCH = [r"\bbatch\b"]
BTN_NO_MORE = [r"no\s*more", r"done", r"finish"]
BTN_SELECT_PROMPT = [r"select\s*prompt", r"choose\s*prompt", r"^prompt", r"\bprompt\b"]
BTN_CONFIRM = [r"confirm", r"\bupload\b", r"\bstart\b", r"✅"]

# Text markers that identify a state (case-insensitive substring match).
TXT_SEND_IMAGE = ["send your image", "image mode", "send all your image", "send your photo"]
TXT_SEND_VIDEO = ["send", "mp4", "video to video"]
TXT_SETTINGS = ["settings", "configure"]
TXT_QUEUED = ["added to queue", "started processing", "processing your", "in queue"]
# Strong failure markers — if a no-media message matches one while we're waiting
# for results, fail fast instead of blocking for the full process_timeout.
TXT_ERROR = ["no face", "not detected", "no person", "couldn't detect", "could not detect",
             "not enough", "insufficient", "buy gems", "try again", "failed", "❌",
             "unsupported", "too large", "invalid", "cancelled", "rejected"]


def _matches(text: str, patterns: Iterable[str]) -> bool:
    t = text or ""
    return any(re.search(p, t, re.IGNORECASE) for p in patterns)


def _contains(text: str, markers: Iterable[str]) -> bool:
    t = (text or "").lower()
    return any(m.lower() in t for m in markers)


@dataclass
class _Capture:
    """Live capture of messages + edits from the bot, fed by Telethon events."""

    client: TelegramClient
    bot: object
    queue: "asyncio.Queue[Message]" = field(default_factory=asyncio.Queue)
    _handlers: list = field(default_factory=list)

    def attach(self) -> None:
        async def on_event(event):
            await self.queue.put(event.message)

        bot_id = getattr(self.bot, "id", None)
        new = events.NewMessage(from_users=bot_id, incoming=True)
        edit = events.MessageEdited(from_users=bot_id, incoming=True)
        self.client.add_event_handler(on_event, new)
        self.client.add_event_handler(on_event, edit)
        self._handlers = [(on_event, new), (on_event, edit)]

    def detach(self) -> None:
        for cb, ev in self._handlers:
            self.client.remove_event_handler(cb, ev)
        self._handlers = []


def _button_texts(msg: Message) -> list[str]:
    out: list[str] = []
    for row in (msg.buttons or []):
        for b in row:
            out.append(b.text)
    return out


def _find_button(msg: Message, patterns: Iterable[str]):
    for row in (msg.buttons or []):
        for b in row:
            if _matches(b.text, patterns):
                return b
    return None


class BotFlow:
    """One conversation with the bot, scoped to a single job."""

    def __init__(self, client: TelegramClient, bot, status: StatusCb | None = None):
        self.client = client
        self.bot = bot
        self._status = status or (lambda _m: None)
        self._cap = _Capture(client, bot)

    def _say(self, msg: str) -> None:
        log.info("[flow] %s", msg)
        self._status(msg)

    # ------------------------------------------------------------------ I/O
    async def __aenter__(self):
        self._cap.attach()
        return self

    async def __aexit__(self, *exc):
        self._cap.detach()
        return False

    async def send_text(self, text: str) -> None:
        log.info("[send] %s", text)
        await self.client.send_message(self.bot, text)

    async def send_media(self, path: str, *, as_document: bool = False) -> None:
        log.info("[send] file %s (document=%s)", os.path.basename(path), as_document)
        kwargs: dict = {}
        ext = os.path.splitext(path)[1].lower()
        is_video = ext in VIDEO_EXTS
        send_path, tmp = path, None
        if is_video and not as_document:
            # Send as a streamable video (with hachoir-detected attributes), else
            # the bot treats it as a document and silently ignores it.
            kwargs["supports_streaming"] = True
        elif ext in IMAGE_EXTS and not as_document:
            # Downscale/re-encode if Telegram would reject it as a photo (>10 MB
            # or too large). _prepare_photo returns the original when it's fine.
            send_path = await asyncio.to_thread(_prepare_photo, path)
            tmp = send_path if send_path != path else None
        try:
            await self.client.send_file(self.bot, send_path, force_document=as_document, **kwargs)
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    async def _drain_next(self, timeout: float) -> Message:
        return await asyncio.wait_for(self._cap.queue.get(), timeout=timeout)

    async def await_state(
        self,
        *,
        button_patterns: list[str] | None = None,
        text_markers: list[str] | None = None,
        want_media: bool = False,
        timeout: float | None = None,
        skip_ids: set[int] | None = None,
    ) -> Message:
        """Pull bot messages until one satisfies the requested predicate."""
        timeout = timeout if timeout is not None else settings.step_timeout
        skip_ids = skip_ids or set()
        deadline = asyncio.get_event_loop().time() + timeout
        last_seen = None
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                opts = _button_texts(last_seen) if last_seen else []
                raise TimeoutError(
                    "Bot did not reach the expected state in time. "
                    f"Wanted: buttons={button_patterns} text={text_markers} media={want_media}. "
                    f"Last message buttons: {opts}"
                )
            try:
                msg = await self._drain_next(remaining)
            except asyncio.TimeoutError:
                continue
            if msg.id in skip_ids:
                continue
            last_seen = msg
            txt = (msg.text or "").replace("\n", " ")[:160]
            log.info("[recv] id=%s media=%s buttons=%s text=%r",
                     msg.id, bool(msg.file), _button_texts(msg), txt)

            ok = True
            if button_patterns is not None:
                ok = ok and _find_button(msg, button_patterns) is not None
            if text_markers is not None:
                ok = ok and _contains(msg.text or "", text_markers)
            if want_media:
                ok = ok and msg.file is not None
            if ok:
                return msg

    async def click(self, msg: Message, patterns: list[str], *, what: str = "") -> str:
        btn = _find_button(msg, patterns)
        if btn is None:
            raise RuntimeError(
                f"Couldn't find a button for {what or patterns}. "
                f"Available buttons: {_button_texts(msg)}"
            )
        self._say(f"click: {btn.text}")
        await btn.click()
        return btn.text

    async def refresh(self, msg: Message) -> Message:
        """Re-fetch a message by id to read its current (edited) button state."""
        fresh = await self.client.get_messages(self.bot, ids=msg.id)
        return fresh or msg

    # ------------------------------------------------------------------ flows
    async def run_images(self, paths: list[str], prompt: str) -> list[str]:
        """image→image. `paths` has 1 (single) or many (batch). Returns output paths."""
        batch = len(paths) > 1
        await self.send_text("/start")
        menu = await self.await_state(button_patterns=BTN_IMG2IMG)
        await self.click(menu, BTN_IMG2IMG, what="image to image V2")

        mode = await self.await_state(button_patterns=BTN_SINGLE + BTN_BATCH)
        await self.click(mode, BTN_BATCH if batch else BTN_SINGLE,
                         what="Batch" if batch else "Single")

        if batch:
            # Each upload is acked ("Image added. Total: N") on a message that
            # carries a single combined "No More Image, Select Prompt" button.
            ack = None
            for i, p in enumerate(paths, 1):
                self._say(f"uploading image {i}/{len(paths)}")
                await self.send_media(p)
                ack = await self.await_state(
                    button_patterns=BTN_NO_MORE + BTN_SELECT_PROMPT,
                    timeout=settings.step_timeout,
                )
            # After the last image, that button opens the prompt menu.
            await self.click(ack, BTN_NO_MORE + BTN_SELECT_PROMPT,
                             what="No More Image / Select Prompt")
            prompt_msg = await self.await_state(
                button_patterns=[re.escape(prompt)], timeout=settings.step_timeout)
        else:
            try:
                await self.await_state(text_markers=TXT_SEND_IMAGE, timeout=settings.step_timeout)
            except TimeoutError:
                log.warning("no 'send your image' prompt; sending anyway")
            self._say("uploading image")
            await self.send_media(paths[0])
            prompt_msg = await self.await_state(
                button_patterns=[re.escape(prompt)], timeout=settings.step_timeout)

        # Choose the prompt (e.g. "Nude").
        await self.click(prompt_msg, [re.escape(prompt)], what=f"prompt={prompt}")
        await self._wait_queued()
        return await self._collect_media(count=len(paths))

    async def run_video(self, path: str, settings_values: dict[str, str]) -> list[str]:
        """video→video. Returns a single-element list with the output path.

        The settings menu (validated live) has these toggle groups; the bot
        marks the active choice with a "[x]" prefix:
            template:   1 2 3 4 5
            breast:     small medium large
            duration:   5s 10s 15s 20s 25s 30s   (each shows its Gem cost!)
            ratio:      9:16  16:9
            resolution: 480p  720p
        We click a value only if it isn't already selected, re-reading the
        (edited) menu after each click.
        """
        await self.send_text("/start")
        menu = await self.await_state(button_patterns=BTN_VID2VID)
        await self.click(menu, BTN_VID2VID, what="video to video V2")

        try:
            await self.await_state(text_markers=TXT_SEND_VIDEO, timeout=settings.step_timeout)
        except TimeoutError:
            log.warning("no 'send mp4' prompt; sending anyway")
        self._say("uploading video")
        await self.send_media(path)

        # The bot replies with a "V2 Preview" media then the settings message.
        cfg = await self.await_state(text_markers=TXT_SETTINGS, timeout=settings.step_timeout)
        for label, value in settings_values.items():
            if not value:
                continue
            # Word-boundary so "1" (template) doesn't match "10s"/"16:9"/etc.
            pat = [rf"(?<![\w]){re.escape(value)}(?![\w])"]
            btn = _find_button(cfg, pat)
            if btn is None:
                log.warning("video setting %s=%r has no button (have %s); skipping",
                            label, value, _button_texts(cfg))
                continue
            if "[x]" in btn.text:
                self._say(f"{label}={value} already selected")
                continue
            await self.click(cfg, pat, what=f"{label}={value}")
            cfg = await self.refresh(cfg)

        await self.click(cfg, BTN_CONFIRM, what="confirm/upload")
        await self._wait_queued()
        return await self._collect_media(count=1)

    # ------------------------------------------------------------------ helpers
    async def _wait_queued(self) -> None:
        try:
            await self.await_state(text_markers=TXT_QUEUED, timeout=settings.step_timeout)
            self._say("queued — bot is processing…")
        except TimeoutError:
            log.warning("no explicit queue/processing message; waiting for media")
            self._say("processing…")

    async def _collect_media(self, count: int) -> list[str]:
        """Wait for `count` media messages, downloading each to download_dir.

        Fails fast if the bot sends a clear error message (no media) while we
        wait, rather than blocking until process_timeout.
        """
        os.makedirs(settings.download_dir, exist_ok=True)
        out: list[str] = []
        seen: set[int] = set()
        deadline = asyncio.get_event_loop().time() + settings.process_timeout
        while len(out) < count:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out waiting for results ({len(out)}/{count} received).")
            try:
                msg = await self._drain_next(remaining)
            except asyncio.TimeoutError:
                continue
            if msg.id in seen:
                continue
            if msg.file is not None:
                seen.add(msg.id)
                dest = await msg.download_media(file=settings.download_dir)
                if not dest:
                    log.warning("media message %s produced no file", msg.id)
                    continue
                self._say(f"received result {len(out) + 1}/{count}")
                out.append(dest)
            else:
                text = (msg.text or "").replace("\n", " ")
                log.info("[recv] (waiting for media) %r buttons=%s", text[:160], _button_texts(msg))
                if _contains(text, TXT_ERROR):
                    raise RuntimeError(f"Bot reported a problem: {text[:200]}")
        return out


class VixalClient:
    """Owns the shared Telethon user session for the app's lifetime."""

    def __init__(self) -> None:
        self._client: TelegramClient | None = None
        self._bot = None
        self._lock = asyncio.Lock()
        # Set when start() failed (e.g. session not authorized) so the API can
        # surface a clear, actionable message instead of a generic crash.
        self.start_error: str | None = None

    @property
    def ready(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        client = TelegramClient(
            settings.telegram_session,
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(
                "Telegram session is not authorized. Run a one-time login first:\n"
                "    python -m scripts.login"
            )
        self._client = client
        self._bot = await client.get_entity(settings.telegram_vixal_ai_bot_username)
        log.info("Telegram user session ready; bot=%s", settings.telegram_vixal_ai_bot_username)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    def _require(self) -> TelegramClient:
        if self._client is None:
            raise RuntimeError(self.start_error or "VixalClient not started")
        return self._client

    async def process_images(self, paths: list[str], prompt: str,
                             status: StatusCb | None = None) -> list[str]:
        async with self._lock:  # one conversation at a time
            async with BotFlow(self._require(), self._bot, status) as flow:
                return await flow.run_images(paths, prompt)

    async def process_video(self, path: str, settings_values: dict[str, str],
                            status: StatusCb | None = None) -> list[str]:
        async with self._lock:
            async with BotFlow(self._require(), self._bot, status) as flow:
                return await flow.run_video(path, settings_values)


client = VixalClient()
