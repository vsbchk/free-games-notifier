"""Conservative public-store parsers, adapted from epic.PY and steam.py."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

EPIC_API = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
STEAM = "https://store.steampowered.com"


def utcnow():
    return datetime.now(timezone.utc)


def date(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone missing")
    return result


@dataclass(frozen=True)
class Offer:
    key: str
    store: str
    title: str
    url: str
    price: str
    end: str = ""
    image: str = ""

    def active(self):
        return not self.end or date(self.end) > utcnow()


def epic_link(game):
    candidates = [m.get("pageSlug") for m in game.get("offerMappings", []) or []]
    candidates += [
        m.get("pageSlug") for m in (game.get("catalogNs") or {}).get("mappings", [])
    ]
    candidates += [game.get("productSlug"), game.get("urlSlug")]
    for slug in candidates:
        if slug:
            slug = slug.strip("/").removeprefix("p/").removesuffix("/home")
            if re.fullmatch(r"[A-Za-z0-9_-]+", slug):
                return f"https://store.epicgames.com/en-US/p/{slug}"
    return ""


def parse_epic(data, now=None):
    now = now or utcnow()
    elements = data["data"]["Catalog"]["searchStore"]["elements"]
    if not isinstance(elements, list):
        raise ValueError("Invalid Epic catalog")
    result = {}
    for game in elements:
        if game.get("offerType") != "BASE_GAME":
            continue
        price = game["price"]["totalPrice"]
        if price["originalPrice"] <= 0 or price["discountPrice"] != 0:
            continue
        url = epic_link(game)
        if not url:
            continue
        for group in (game.get("promotions") or {}).get("promotionalOffers", []):
            for promo in group["promotionalOffers"]:
                if promo["discountSetting"]["discountPercentage"] != 0:
                    continue
                start, end = date(promo["startDate"]), date(promo["endDate"])
                if not start <= now < end:
                    continue
                key = f"epic:{game['namespace']}:{game['id']}:{start.isoformat()}"
                image = next(
                    (
                        i["url"]
                        for i in game.get("keyImages", [])
                        if i["type"] in {"OfferImageWide", "DieselStoreFrontWide"}
                    ),
                    "",
                )
                original = (price.get("fmtPrice") or {}).get(
                    "originalPrice"
                ) or "Не указана"
                result[key] = Offer(
                    key,
                    "epic",
                    game["title"],
                    url,
                    f"{original} {price.get('currencyCode', '')}".strip(),
                    end.isoformat(),
                    image,
                )
    return list(result.values())


def steam_candidates(html):
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("#search_resultsRows") is None:
        raise ValueError("Steam search markup missing (or access blocked)")
    ids = set()
    for row in soup.select("a.search_result_row"):
        discount = row.select_one(".discount_pct")
        appid = row.get("data-ds-appid", "")
        if discount and discount.get_text(strip=True) == "-100%" and appid.isdigit():
            ids.add(appid)
    return ids


def parse_steam(appid, payload, html):
    entry = payload[appid]
    if entry.get("success") is not True:
        raise ValueError("Steam appdetails unavailable")
    game = entry["data"]
    if game.get("type") != "game" or game.get("is_free"):
        return None
    price = game.get("price_overview") or {}
    if not (
        price.get("initial", 0) > 0
        and price.get("final") == 0
        and price.get("discount_percent") == 100
    ):
        return None
    soup = BeautifulSoup(html, "html.parser")
    area = soup.select_one("#game_area_purchase")
    if area is None:
        raise ValueError("Steam purchase area unavailable (age gate or changed markup)")
    text = area.get_text(" ", strip=True)
    if re.search(r"free weekend|play for free|free trial|temporary access", text, re.I):
        return None
    if not re.search(r"free to keep|keep (?:it|this game) forever", text, re.I):
        return None
    end_node = area.select_one("[data-discount-enddate]")
    end = ""
    if end_node:
        end = datetime.fromtimestamp(
            int(end_node["data-discount-enddate"]), timezone.utc
        ).isoformat()
    # Deadlines can disappear or change; do not turn metadata edits into new offers.
    key = f"steam:{appid}"
    offer = Offer(
        key,
        "steam",
        game["name"],
        f"{STEAM}/app/{appid}/",
        price.get("initial_formatted") or "Не указана",
        end,
        game.get("header_image", ""),
    )
    return offer if offer.active() else None


class Stores:
    def __init__(self, country, stop):
        self.country, self.stop = country, stop

    def get(self, url, **kwargs):
        if self.stop.is_set():
            raise InterruptedError
        response = requests.get(
            url,
            timeout=(10, 25),
            headers={"User-Agent": "free-games-notifier/3.0"},
            **kwargs,
        )
        response.raise_for_status()
        return response

    def epic(self):
        return parse_epic(
            self.get(
                EPIC_API,
                params={
                    "locale": "en-US",
                    "country": self.country,
                    "allowCountries": self.country,
                },
            ).json()
        )

    def steam(self):
        ids = set()
        for page in range(1, 21):
            html = self.get(
                f"{STEAM}/search/",
                params={
                    "maxprice": "free",
                    "specials": 1,
                    "cc": self.country,
                    "l": "english",
                    "page": page,
                    "count": 100,
                    "ignore_preferences": 1,
                },
            ).text
            ids.update(steam_candidates(html))
            soup = BeautifulSoup(html, "html.parser")
            if not any(
                a.get_text(strip=True) in {">", "›", "→"}
                for a in soup.select(".search_pagination_right a")
            ):
                break
        else:
            raise ValueError("Steam pagination limit reached")
        result = []
        for appid in sorted(ids):
            payload = self.get(
                f"{STEAM}/api/appdetails",
                params={"appids": appid, "cc": self.country, "l": "english"},
            ).json()
            html = self.get(
                f"{STEAM}/app/{appid}/", params={"cc": self.country, "l": "english"}
            ).text
            offer = parse_steam(appid, payload, html)
            if offer:
                result.append(offer)
        return result
