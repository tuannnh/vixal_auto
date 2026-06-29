"""Authorize the Telethon user session — non-interactive, two steps.

The bot is driven by *your* Telegram account, so we log in once and save a
`*.session` file the webapp reuses headlessly. This environment has no
interactive stdin, so instead of prompting we read the code / 2FA password from
environment variables, in two steps:

    # 1) ask Telegram to send you a login code (arrives in your Telegram app):
    .venv/bin/python -m scripts.login request

    # 2) sign in with that code (and your 2FA password, if you have one):
    VIXAL_LOGIN_CODE=12345 .venv/bin/python -m scripts.login signin
    VIXAL_LOGIN_CODE=12345 VIXAL_LOGIN_PASSWORD=hunter2 .venv/bin/python -m scripts.login signin

Between the two, a small `.login_state.json` holds the phone_code_hash Telegram
needs to verify the code; it's deleted on success.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from app.config import settings

STATE_FILE = ".login_state.json"


def _client() -> TelegramClient:
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")
    return TelegramClient(
        settings.telegram_session,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )


async def _confirm_bot(client: TelegramClient) -> None:
    bot = settings.telegram_vixal_ai_bot_username
    try:
        ent = await client.get_entity(bot)
        print(f"✅ Bot reachable: @{getattr(ent, 'username', bot)} (id={ent.id}).")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  Could not resolve @{bot}: {exc}. "
              f"Open a chat with the bot once in Telegram, then retry.")


async def request() -> None:
    if not settings.telegram_phone:
        raise SystemExit("Set TELEGRAM_PHONE in .env first (e.g. +8490…).")
    client = _client()
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("✅ Already authorized — nothing to do.")
            return
        sent = await client.send_code_request(settings.telegram_phone)
        with open(STATE_FILE, "w") as f:
            json.dump({"phone": settings.telegram_phone, "hash": sent.phone_code_hash}, f)
        print("📨 A login code was sent to your Telegram app. Now run:\n")
        print("    VIXAL_LOGIN_CODE=<code> .venv/bin/python -m scripts.login signin")
        print("    (add VIXAL_LOGIN_PASSWORD=<2fa> if your account has 2FA)")
    finally:
        await client.disconnect()


async def signin() -> None:
    code = os.environ.get("VIXAL_LOGIN_CODE", "").strip()
    if not code:
        raise SystemExit("Set VIXAL_LOGIN_CODE=<code> (run `… login request` first).")
    if not os.path.exists(STATE_FILE):
        raise SystemExit(f"No {STATE_FILE}; run `… login request` first.")
    with open(STATE_FILE) as f:
        state = json.load(f)

    client = _client()
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("✅ Already authorized.")
        else:
            try:
                await client.sign_in(
                    phone=state["phone"], code=code, phone_code_hash=state["hash"]
                )
            except SessionPasswordNeededError:
                pw = os.environ.get("VIXAL_LOGIN_PASSWORD", "")
                if not pw:
                    raise SystemExit(
                        "Account has 2FA. Re-run with "
                        "VIXAL_LOGIN_PASSWORD=<password> (and VIXAL_LOGIN_CODE).")
                await client.sign_in(password=pw)

        me = await client.get_me()
        print(f"✅ Authorized as {me.first_name} (@{me.username}).")
        print(f"   Session saved to: {settings.telegram_session}")
        await _confirm_bot(client)
        try:
            os.remove(STATE_FILE)
        except OSError:
            pass
    finally:
        await client.disconnect()


async def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "request"
    if cmd == "request":
        await request()
    elif cmd == "signin":
        await signin()
    else:
        raise SystemExit("Usage: python -m scripts.login [request|signin]")


if __name__ == "__main__":
    asyncio.run(main())
