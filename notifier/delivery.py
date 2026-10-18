import math
from zoneinfo import ZoneInfo

import requests

from .stores import date


class DeliveryError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class Delivery:
    def __init__(self, settings):
        self.settings = settings

    def description(self, offer):
        end = (
            date(offer.end)
            .astimezone(ZoneInfo(self.settings.timezone))
            .strftime("%Y-%m-%d %H:%M %Z")
            if offer.end
            else "Не указана"
        )
        return f"Магазин: {offer.store}\nБесплатно навсегда\nОбычная цена: {offer.price}\nОкончание: {end}"

    def post(self, url, payload, **kwargs):
        response = requests.post(
            url, json=payload, timeout=(10, 25), allow_redirects=False, **kwargs
        )
        if response.status_code == 429:
            delay = 60
            try:
                data = response.json()
                raw = (data.get("parameters") or {}).get(
                    "retry_after", data.get("retry_after", 60)
                )
                parsed = float(raw)
                if math.isfinite(parsed) and parsed > 0:
                    delay = parsed
            except (ValueError, TypeError, AttributeError):
                pass
            raise DeliveryError("Rate limited", delay)
        return response

    def telegram(self, offer):
        base = f"https://api.telegram.org/bot{self.settings.telegram_token}/"
        text = f"{offer.title[:200]}\n{self.description(offer)}\n{offer.url}"
        payload = {"chat_id": self.settings.telegram_chat}
        if offer.image and len(text) <= 1024:
            response = self.post(
                base + "sendPhoto", {**payload, "photo": offer.image, "caption": text}
            )
            if response.status_code == 400:
                response = self.post(base + "sendMessage", {**payload, "text": text})
        else:
            response = self.post(base + "sendMessage", {**payload, "text": text})
        if response.status_code != 200 or response.json().get("ok") is not True:
            raise DeliveryError(
                f"Telegram rejected request (HTTP {response.status_code})"
            )

    def discord(self, offer):
        embed = {
            "title": offer.title[:256],
            "url": offer.url,
            "description": self.description(offer),
            "color": 0x22C55E,
        }
        if offer.image:
            embed["image"] = {"url": offer.image}
        response = self.post(
            self.settings.discord_webhook,
            {"embeds": [embed], "allowed_mentions": {"parse": []}},
            params={"wait": "true"},
        )
        if response.status_code != 200 or not response.json().get("id"):
            raise DeliveryError(
                f"Discord rejected request (HTTP {response.status_code})"
            )
