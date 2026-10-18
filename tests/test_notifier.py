import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
import yaml

from notifier.config import Settings
from notifier.delivery import Delivery, DeliveryError
from notifier.service import cycle, main
from notifier.state import State
from notifier.stores import Offer, Stores, parse_epic, parse_steam, steam_candidates

ENV = {
    "TELEGRAM_BOT_TOKEN": "123:offline",
    "TELEGRAM_CHAT_ID": "-100123",
    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/offline",
}
OFFER = Offer(
    "epic:id:promo",
    "epic",
    "Test Game",
    "https://store.epicgames.com/en-US/p/test",
    "19.99 USD",
    "2099-01-01T00:00:00+00:00",
    "https://example.org/cover.jpg",
)
NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def epic_fixture():
    return {
        "data": {
            "Catalog": {
                "searchStore": {
                    "elements": [
                        {
                            "namespace": "ns",
                            "id": "id",
                            "title": "Test Game",
                            "offerType": "BASE_GAME",
                            "offerMappings": [{"pageSlug": "test-game"}],
                            "price": {
                                "totalPrice": {
                                    "originalPrice": 1999,
                                    "discountPrice": 0,
                                    "currencyCode": "USD",
                                    "fmtPrice": {"originalPrice": "$19.99"},
                                }
                            },
                            "keyImages": [
                                {
                                    "type": "OfferImageWide",
                                    "url": "https://example.org/cover.jpg",
                                }
                            ],
                            "promotions": {
                                "promotionalOffers": [
                                    {
                                        "promotionalOffers": [
                                            {
                                                "discountSetting": {
                                                    "discountPercentage": 0
                                                },
                                                "startDate": "2026-10-15T00:00:00Z",
                                                "endDate": "2026-10-22T00:00:00Z",
                                            }
                                        ]
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        }
    }


def steam_fixture():
    return {
        "123": {
            "success": True,
            "data": {
                "name": "Test Game",
                "type": "game",
                "is_free": False,
                "price_overview": {
                    "initial": 1999,
                    "final": 0,
                    "discount_percent": 100,
                    "initial_formatted": "$19.99",
                },
                "header_image": "https://example.org/cover.jpg",
            },
        }
    }


def purchase(text="Free to keep when you get it before the deadline"):
    return f'<div id="game_area_purchase">{text}</div>'


class ConfigTests(unittest.TestCase):
    def test_defaults_and_secret_repr(self):
        settings = Settings.from_env(ENV)
        self.assertEqual(settings.interval, 60)
        self.assertTrue(settings.notify_existing)
        self.assertNotIn("offline", repr(settings))

    def test_missing_empty_and_malformed_values(self):
        cases = [(key, "") for key in ENV] + [
            ("TELEGRAM_BOT_TOKEN", "secret"),
            ("TELEGRAM_CHAT_ID", "1,2"),
            ("DISCORD_WEBHOOK_URL", "https://evil.example/secret"),
            ("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x?wait=false"),
            ("CHECK_INTERVAL_MINUTES", "0"),
            ("CHECK_INTERVAL_MINUTES", "NaN"),
            ("CHECK_INTERVAL_MINUTES", "1441"),
            ("CHECK_INTERVAL_MINUTES", ""),
            ("NOTIFY_EXISTING_ON_START", "maybe"),
            ("TZ", "not/a/zone"),
            ("TZ", "/etc/passwd"),
            ("STORE_COUNTRY", "POL"),
            ("STATE_DIR", " "),
        ]
        for key, value in cases:
            with (
                self.subTest(key=key, value=value),
                self.assertRaises(ValueError) as ctx,
            ):
                Settings.from_env({**ENV, key: value})
            self.assertNotIn("evil.example", str(ctx.exception))
        with self.assertRaises(ValueError):
            Settings.from_env({})

    def test_false_and_custom_settings(self):
        settings = Settings.from_env(
            {
                **ENV,
                "NOTIFY_EXISTING_ON_START": "false",
                "CHECK_INTERVAL_MINUTES": "10",
                "TZ": "Europe/Moscow",
                "STORE_COUNTRY": "us",
            }
        )
        self.assertFalse(settings.notify_existing)
        self.assertEqual(settings.country, "US")
        self.assertEqual(settings.interval, 10)


class ParserTests(unittest.TestCase):
    def test_epic_confirmed_active_offer(self):
        offers = parse_epic(epic_fixture(), NOW)
        self.assertEqual(len(offers), 1)
        self.assertIn("ns:id:", offers[0].key)
        self.assertEqual(offers[0].price, "$19.99 USD")
        self.assertTrue(offers[0].image)

    def test_epic_rejects_f2p_dlc_demo_and_nonzero_price(self):
        for field, value in [
            ("offerType", "DLC"),
            ("offerType", "DEMO"),
            ("originalPrice", 0),
            ("discountPrice", 100),
        ]:
            data = epic_fixture()
            game = data["data"]["Catalog"]["searchStore"]["elements"][0]
            (game if field == "offerType" else game["price"]["totalPrice"])[field] = (
                value
            )
            with self.subTest(field=field, value=value):
                self.assertEqual(parse_epic(data, NOW), [])

    def test_epic_only_current_window_and_all_groups(self):
        data = epic_fixture()
        promos = data["data"]["Catalog"]["searchStore"]["elements"][0]["promotions"]
        promos["promotionalOffers"].insert(0, {"promotionalOffers": []})
        self.assertEqual(len(parse_epic(data, NOW)), 1)
        self.assertEqual(
            parse_epic(data, datetime(2026, 10, 22, tzinfo=timezone.utc)), []
        )
        self.assertEqual(
            parse_epic(data, datetime(2026, 10, 14, tzinfo=timezone.utc)), []
        )
        promos["upcomingPromotionalOffers"] = promos.pop("promotionalOffers")
        self.assertEqual(parse_epic(data, NOW), [])

    def test_epic_invalid_response_raises(self):
        for data in ({}, {"data": {"Catalog": {"searchStore": {"elements": None}}}}):
            with self.assertRaises((KeyError, ValueError)):
                parse_epic(data, NOW)

    def test_steam_search_requires_100_percent_and_appid(self):
        html = '<div id="search_resultsRows">'
        for appid, discount in [
            ("123", "-100%"),
            ("456", "-50%"),
            ("7,8", "-100%"),
            ("9", ""),
        ]:
            html += f'<a class="search_result_row" data-ds-appid="{appid}"><div class="discount_pct">{discount}</div></a>'
        self.assertEqual(steam_candidates(html + "</div>"), {"123"})
        self.assertEqual(steam_candidates('<div id="search_resultsRows"></div>'), set())
        with self.assertRaises(ValueError):
            steam_candidates("Access denied")

    def test_steam_confirmed_free_to_keep(self):
        offer = parse_steam("123", steam_fixture(), purchase())
        self.assertEqual(offer.store, "steam")
        self.assertEqual(offer.price, "$19.99")
        self.assertEqual(offer.end, "")

    def test_steam_rejects_unconfirmed_and_temporary_access(self):
        for text in (
            "Free Weekend",
            "Play for free",
            "Free trial",
            "Free",
            "-100%",
            "Free to keep. Free weekend",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_steam("123", steam_fixture(), purchase(text)))
        self.assertIsNone(
            parse_steam("123", steam_fixture(), purchase("Get it") + "Free to keep")
        )

    def test_steam_rejects_f2p_demo_dlc_and_bad_price(self):
        for field, value in [
            ("type", "demo"),
            ("type", "dlc"),
            ("is_free", True),
            ("initial", 0),
            ("final", 1),
            ("discount_percent", 99),
        ]:
            data = steam_fixture()
            game = data["123"]["data"]
            (game if field in {"type", "is_free"} else game["price_overview"])[
                field
            ] = value
            with self.subTest(field=field):
                self.assertIsNone(parse_steam("123", data, purchase()))

    def test_steam_expired_and_future_deadline(self):
        html = purchase('Free to keep <div data-discount-enddate="1"></div>')
        self.assertIsNone(parse_steam("123", steam_fixture(), html))
        html = purchase('Free to keep <div data-discount-enddate="4070908800"></div>')
        self.assertIn("2099", parse_steam("123", steam_fixture(), html).end)
        self.assertEqual(
            parse_steam("123", steam_fixture(), html).key,
            parse_steam("123", steam_fixture(), purchase()).key,
        )

    def test_steam_bad_api_and_age_gate(self):
        with self.assertRaises(ValueError):
            parse_steam("123", {"123": {"success": False}}, purchase())
        with self.assertRaises(ValueError):
            parse_steam("123", steam_fixture(), "Age gate")

    @patch("notifier.stores.requests.get")
    def test_external_http_errors_and_timeouts(self, get):
        stores = Stores("PL", threading.Event())
        get.return_value.raise_for_status.side_effect = requests.HTTPError("private")
        with self.assertRaises(requests.HTTPError):
            stores.epic()
        get.side_effect = requests.Timeout("private")
        with self.assertRaises(requests.Timeout):
            stores.steam()

    def test_stop_prevents_requests(self):
        stop = threading.Event()
        stop.set()
        with (
            patch("notifier.stores.requests.get") as get,
            self.assertRaises(InterruptedError),
        ):
            Stores("PL", stop).epic()
        get.assert_not_called()

    def test_steam_pagination(self):
        stores = Stores("PL", threading.Event())
        row = '<a class="search_result_row" data-ds-appid="123"><div class="discount_pct">-100%</div></a>'
        first = '<div id="search_resultsRows"></div><div class="search_pagination_right"><a>&gt;</a></div>'
        stores.get = Mock(
            side_effect=[
                SimpleNamespace(text=first),
                SimpleNamespace(text=f'<div id="search_resultsRows">{row}</div>'),
                Mock(json=lambda: steam_fixture()),
                SimpleNamespace(text=purchase()),
            ]
        )
        self.assertEqual(len(stores.steam()), 1)
        self.assertEqual(stores.get.call_count, 4)


class StateAndCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=".")
        self.directory = Path(self.temp.name)
        self.state = State(self.directory)
        self.settings = Settings.from_env(ENV)
        self.sources = SimpleNamespace(
            epic=Mock(return_value=[OFFER]), steam=Mock(return_value=[])
        )
        self.delivery = SimpleNamespace(telegram=Mock(), discord=Mock())
        self.stop = threading.Event()

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def run_cycle(self):
        cycle(self.settings, self.state, self.sources, self.delivery, self.stop)

    def test_duplicate_prevention_and_restart(self):
        self.run_cycle()
        self.state.close()
        self.state = State(self.directory)
        self.run_cycle()
        self.delivery.telegram.assert_called_once()
        self.delivery.discord.assert_called_once()

    def test_failed_channel_retries_without_duplicate_other_channel(self):
        for channel in ("telegram", "discord"):
            with self.subTest(channel=channel):
                offer = replace(OFFER, key=channel)
                self.sources.epic.return_value = [offer]
                failed = getattr(self.delivery, channel)
                failed.side_effect = requests.Timeout("SECRET_URL")
                with self.assertLogs("notifier", level="WARNING") as logs:
                    self.run_cycle()
                self.assertNotIn("SECRET_URL", " ".join(logs.output))
                self.assertTrue(self.state.pending(offer, channel))
                other = "discord" if channel == "telegram" else "telegram"
                self.assertFalse(self.state.pending(offer, other))
                other_calls = getattr(self.delivery, other).call_count
                self.state.close()
                self.state = State(self.directory)
                failed.side_effect = None
                self.run_cycle()
                self.assertFalse(self.state.pending(offer, channel))
                self.assertEqual(getattr(self.delivery, other).call_count, other_calls)

    def test_baseline_is_persistent_and_per_store(self):
        self.settings = replace(self.settings, notify_existing=False)
        self.sources.steam.side_effect = requests.Timeout()
        self.run_cycle()
        self.delivery.telegram.assert_not_called()
        self.state.close()
        self.state = State(self.directory)
        self.sources.steam.side_effect = None
        self.sources.steam.return_value = [
            replace(OFFER, key="steam:old", store="steam")
        ]
        self.run_cycle()
        self.delivery.telegram.assert_not_called()
        self.sources.epic.return_value = [OFFER, replace(OFFER, key="epic:new")]
        self.run_cycle()
        self.delivery.telegram.assert_called_once()

    def test_initial_empty_catalog_then_new_offer(self):
        self.settings = replace(self.settings, notify_existing=False)
        self.sources.epic.return_value = []
        self.run_cycle()
        self.sources.epic.return_value = [OFFER]
        self.run_cycle()
        self.delivery.telegram.assert_called_once()

    def test_either_store_can_fail_independently(self):
        for failed in ("epic", "steam"):
            other = "steam" if failed == "epic" else "epic"
            getattr(self.sources, failed).side_effect = requests.Timeout()
            getattr(self.sources, other).side_effect = None
            getattr(self.sources, other).return_value = [
                replace(OFFER, key=other, store=other)
            ]
            self.run_cycle()
        self.assertEqual(self.delivery.telegram.call_count, 2)

    def test_no_retry_of_unconfirmed_or_expired_offers(self):
        self.delivery.telegram.side_effect = requests.Timeout()
        self.run_cycle()
        self.sources.epic.side_effect = requests.Timeout()
        self.run_cycle()
        self.sources.epic.side_effect = None
        self.sources.epic.return_value = [replace(OFFER, end="2000-01-01T00:00:00Z")]
        self.run_cycle()
        self.delivery.telegram.assert_called_once()

    def test_database_error_is_fatal(self):
        with patch.object(self.state, "delivered", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_cycle()

    def test_rate_limit_persists_across_restart(self):
        self.delivery.telegram.side_effect = DeliveryError(
            "Rate limited", retry_after=7200
        )
        self.run_cycle()
        self.state.close()
        self.state = State(self.directory)
        self.run_cycle()
        self.delivery.telegram.assert_called_once()
        self.delivery.discord.assert_called_once()
        self.assertTrue(self.state.pending(OFFER, "telegram"))
        self.state.defer("telegram", 0)
        self.delivery.telegram.side_effect = None
        self.run_cycle()
        self.assertFalse(self.state.pending(OFFER, "telegram"))

    def test_corrupt_database_is_not_reset(self):
        self.state.close()
        (self.directory / "state.sqlite3").write_bytes(b"not a database" * 100)
        with self.assertRaises(sqlite3.DatabaseError):
            State(self.directory)
        # tearDown may close an already closed connection safely.

    def test_second_instance_cannot_use_same_state(self):
        with self.assertRaises(OSError):
            State(self.directory)
        self.run_cycle()
        self.state.close()
        self.state = State(self.directory)
        self.run_cycle()
        self.delivery.telegram.assert_called_once()

    def test_delivery_runs_concurrently(self):
        barrier = threading.Barrier(2)
        self.delivery.telegram.side_effect = lambda _: barrier.wait(timeout=2)
        self.delivery.discord.side_effect = lambda _: barrier.wait(timeout=2)
        self.run_cycle()
        self.assertFalse(self.state.pending(OFFER, "telegram"))
        self.assertFalse(self.state.pending(OFFER, "discord"))

    def test_stop_avoids_delivery(self):
        self.stop.set()
        self.run_cycle()
        self.delivery.telegram.assert_not_called()


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.delivery = Delivery(Settings.from_env(ENV))
        self.delivery.post = Mock()

    def response(self, status, data):
        return Mock(status_code=status, json=lambda: data)

    def test_telegram_photo_and_definite_failure_fallback(self):
        self.delivery.post.side_effect = [
            self.response(400, {}),
            self.response(200, {"ok": True}),
        ]
        self.delivery.telegram(OFFER)
        calls = self.delivery.post.call_args_list
        self.assertTrue(calls[0].args[0].endswith("sendPhoto"))
        self.assertTrue(calls[1].args[0].endswith("sendMessage"))
        self.assertIn("19.99 USD", calls[1].args[1]["text"])

    def test_telegram_timeout_does_not_fallback_immediately(self):
        self.delivery.post.side_effect = requests.Timeout()
        with self.assertRaises(requests.Timeout):
            self.delivery.telegram(OFFER)
        self.delivery.post.assert_called_once()

    def test_telegram_rejection_and_rate_limit(self):
        for status, data in [(200, {"ok": False}), (429, {}), (403, {}), (500, {})]:
            self.delivery.post.return_value = self.response(status, data)
            with self.assertRaises(DeliveryError):
                self.delivery.telegram(replace(OFFER, image=""))

    def test_discord_requires_message_confirmation(self):
        self.delivery.post.return_value = self.response(200, {"id": "message"})
        self.delivery.discord(OFFER)
        call = self.delivery.post.call_args
        self.assertEqual(call.kwargs["params"], {"wait": "true"})
        self.assertEqual(call.args[1]["allowed_mentions"], {"parse": []})
        for status in (204, 429, 500):
            self.delivery.post.return_value = self.response(status, {})
            with self.assertRaises(DeliveryError):
                self.delivery.discord(OFFER)

    @patch("notifier.delivery.requests.post")
    def test_retry_after_parsing_and_redirect_protection(self, post):
        delivery = Delivery(Settings.from_env(ENV))
        for data, expected in [
            ({"parameters": {"retry_after": 123}}, 123),
            ({"retry_after": 45.5}, 45.5),
            ({"retry_after": "NaN"}, 60),
        ]:
            post.return_value = self.response(429, data)
            with self.assertRaises(DeliveryError) as ctx:
                delivery.discord(OFFER)
            self.assertEqual(ctx.exception.retry_after, expected)
            self.assertFalse(post.call_args.kwargs["allow_redirects"])


class DeploymentTests(unittest.TestCase):
    def test_compose_and_workflow_contract(self):
        compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8-sig"))
        service = compose["services"]["free-games-notifier"]
        self.assertEqual(service["image"], "ghcr.io/vsbchk/free-games-notifier:latest")
        self.assertIn("notifier-state:/data", service["volumes"])
        self.assertEqual(service["environment"]["STATE_DIR"], "/data")
        self.assertTrue(service["read_only"])
        for path in Path(".github/workflows").glob("*.yml"):
            text = path.read_text(encoding="utf-8-sig")
            workflow = yaml.load(text, Loader=yaml.BaseLoader)
            self.assertNotIn("schedule", workflow["on"])
            for forbidden in (
                "secrets.TELEGRAM",
                "secrets.DISCORD",
                "python epic.PY",
                "python steam.py",
            ):
                self.assertNotIn(forbidden, text)

    @patch.dict("os.environ", {}, clear=True)
    def test_invalid_config_exits_without_network(self):
        with patch("notifier.service.Stores") as stores, self.assertLogs("notifier"):
            self.assertEqual(main(), 1)
        stores.assert_not_called()

    def test_shutdown_signal_interrupts_idle_wait(self):
        handlers = {}

        def register(sig, handler):
            handlers[sig] = handler

        def one_cycle(*args):
            import signal

            handlers[signal.SIGTERM](None, None)

        with (
            patch.dict("os.environ", ENV, clear=True),
            patch("notifier.service.signal.signal", register),
            patch("notifier.service.State") as state,
            patch("notifier.service.cycle", side_effect=one_cycle) as run,
        ):
            self.assertEqual(main(), 0)
        run.assert_called_once()
        state.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
