import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Settings:
    telegram_token: str = field(repr=False)
    telegram_chat: str = field(repr=False)
    discord_webhook: str = field(repr=False)
    interval: int = 60
    notify_existing: bool = True
    timezone: str = "Europe/Warsaw"
    country: str = "PL"
    state_dir: Path = Path("data")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env

        def value(key, default=""):
            result = env.get(key, default).strip()
            if not result:
                raise ValueError(f"{key} must not be empty")
            return result

        token = value("TELEGRAM_BOT_TOKEN")
        chat = value("TELEGRAM_CHAT_ID")
        webhook = value("DISCORD_WEBHOOK_URL")
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Invalid TELEGRAM_BOT_TOKEN format")
        if not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]+", chat):
            raise ValueError("Invalid TELEGRAM_CHAT_ID; specify one group")
        if not re.fullmatch(
            r"https://discord\.com/api/(?:v\d+/)?webhooks/\d+/[A-Za-z0-9_-]+", webhook
        ):
            raise ValueError(
                "Invalid DISCORD_WEBHOOK_URL; use a discord.com webhook URL"
            )
        try:
            interval = int(value("CHECK_INTERVAL_MINUTES", "60"))
            if not 1 <= interval <= 1440:
                raise ValueError
        except ValueError:
            raise ValueError(
                "CHECK_INTERVAL_MINUTES must be an integer from 1 to 1440"
            ) from None
        notify = value("NOTIFY_EXISTING_ON_START", "true").lower()
        if notify not in {"true", "false"}:
            raise ValueError("NOTIFY_EXISTING_ON_START must be true or false")
        tz = value("TZ", "Europe/Warsaw")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Invalid TZ; use an IANA timezone") from None
        country = value("STORE_COUNTRY", "PL").upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("STORE_COUNTRY must contain two ASCII letters")
        return cls(
            token,
            chat,
            webhook,
            interval,
            notify == "true",
            tz,
            country,
            Path(value("STATE_DIR", "data")),
        )
