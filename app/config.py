from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # ------------------------------------------------------------------ Telegram
    # MTProto credentials from https://my.telegram.org. These authorize a *user*
    # session (your own account) that talks to the bot — not a bot token.
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    telegram_phone: str = ""

    # The bot we drive, e.g. "vixal_i_clone_bot" (no @).
    telegram_vixal_ai_bot_username: str = "vixal_i_clone_bot"

    # Telethon session file. Created once by `python -m scripts.login`, then
    # reused. Keep it secret — it's a live login to your Telegram account.
    telegram_session: str = "vixal_auto.session"

    # ------------------------------------------------------------------ copytele
    # Base URL of the copytele/copyparty folder results are written into. A
    # per-kind subfolder (photos/ videos/) is appended automatically. Must end
    # "/". Behind Cloudflare large uploads can 524 — prefer the LAN origin here.
    copytele_upload_url: str = "http://10.1.1.99:11117/source/vixal/"

    # Optional copyparty password. Empty for an open/no-auth volume.
    copytele_pw: str = ""

    # Replace a same-named file on copytele instead of auto-renaming.
    overwrite: bool = False

    # ------------------------------------------------------------------ runtime
    # Where uploads + processed results are buffered. Cleared after each job.
    download_dir: str = "/tmp/vixal_auto"

    # How long (seconds) to wait for the bot to finish a single job (the bot
    # queues + renders, which can take minutes), and how long for each menu step.
    process_timeout: float = 900.0
    step_timeout: float = 120.0

    # Keep at most this many finished jobs in memory for status lookups.
    max_jobs: int = 200

    # Server bind. Behind nginx-proxy-manager you typically expose this port.
    host: str = "0.0.0.0"
    port: int = 8090

    @property
    def upload_base(self) -> str:
        u = self.copytele_upload_url
        return u if u.endswith("/") else u + "/"


settings = Settings()
