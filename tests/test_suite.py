"""Offline test suite. Run: python -m unittest discover -s tests

No network: schedule + market providers are stubbed; outbound HTTP is mocked.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core
import notify
import predict
from config import TelegramConfig
from polymarket import MarketSnapshot
from schedule_source import Match


class StubSchedule:
    def __init__(self, matches):
        self._m = matches

    def matches_on(self, date):
        return self._m


class StubMarket:
    def __init__(self, mapping):
        # mapping: (team1, team2) -> MarketSnapshot | None
        self._map = mapping

    def find_market(self, team1, team2, debug=False):
        return self._map.get((team1, team2))


class TestPoisson(unittest.TestCase):
    def test_favourite_does_not_lose(self):
        p = predict.build_prediction("A", "B", 0.65, 0.22, 0.13)
        self.assertGreaterEqual(p.scoreline[0], p.scoreline[1])

    def test_even_draws(self):
        p = predict.build_prediction("A", "B", 0.33, 0.34, 0.33)
        self.assertEqual(p.scoreline[0], p.scoreline[1])

    def test_devig_sums_to_one(self):
        tw = predict.devig_three_way(0.5, 0.3, 0.4)
        self.assertAlmostEqual(tw.p1 + tw.draw + tw.p2, 1.0, places=9)

    def test_totals_lifts_goals(self):
        low = predict.build_prediction("A", "B", 0.45, 0.25, 0.30)
        high = predict.build_prediction(
            "A", "B", 0.45, 0.25, 0.30, totals=predict.Totals(2.5, 0.85, 0.15)
        )
        self.assertGreater(high.lambda1 + high.lambda2, low.lambda1 + low.lambda2)


class TestCore(unittest.TestCase):
    def test_build_items_mixed(self):
        m1 = Match(date="2026-06-15", team1="Brazil", team2="Croatia", group="C")
        m2 = Match(date="2026-06-15", team1="USA", team2="Wales", group="B")
        snap = MarketSnapshot(
            team1="Brazil", team2="Croatia", p1=0.6, draw=0.25, p2=0.15
        )
        items = core.build_items(
            "2026-06-15",
            schedule=StubSchedule([m1, m2]),
            market_client=StubMarket({("Brazil", "Croatia"): snap}),
        )
        self.assertEqual(len(items), 2)
        self.assertIsNotNone(items[0].prediction)
        self.assertIsNone(items[1].prediction)
        self.assertEqual(items[1].note, "no live market found")

    def test_build_items_isolates_market_error(self):
        m = Match(date="2026-06-15", team1="Brazil", team2="Croatia")

        class Boom:
            def find_market(self, *a, **k):
                raise RuntimeError("network down")

        items = core.build_items(
            "2026-06-15", schedule=StubSchedule([m]), market_client=Boom()
        )
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0].prediction)
        self.assertIn("market lookup failed", items[0].note)


class TestTelegram(unittest.TestCase):
    def test_unconfigured_raises(self):
        with self.assertRaises(RuntimeError):
            notify.send_telegram(TelegramConfig(token="", chat_id=""), "hi")

    def test_posts_to_bot_api(self):
        cfg = TelegramConfig(token="TOK123", chat_id="999")
        with mock.patch.object(notify.requests, "post") as post:
            post.return_value = mock.Mock(raise_for_status=mock.Mock())
            notify.send_telegram(cfg, "hello")
            url = post.call_args.args[0]
            payload = post.call_args.kwargs["json"]
            self.assertIn("botTOK123/sendMessage", url)
            self.assertEqual(payload["chat_id"], "999")
            self.assertEqual(payload["text"], "hello")


class TestWebapp(unittest.TestCase):
    def setUp(self):
        import webapp

        self.webapp = webapp
        self.client = webapp.app.test_client()

    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)

    def test_bad_date_400(self):
        r = self.client.get("/day/not-a-date")
        self.assertEqual(r.status_code, 400)

    def test_day_renders_with_stub(self):
        m = Match(date="2026-06-15", team1="Brazil", team2="Croatia", group="C")
        snap = MarketSnapshot("Brazil", "Croatia", 0.6, 0.25, 0.15)
        items = core.build_items(
            "2026-06-15",
            schedule=StubSchedule([m]),
            market_client=StubMarket({("Brazil", "Croatia"): snap}),
        )
        with mock.patch.object(self.webapp.core, "build_items", return_value=items), \
             mock.patch.object(self.webapp, "_accuracy_for", return_value=None):
            r = self.client.get("/day/2026-06-15")
            self.assertEqual(r.status_code, 200)
            self.assertIn(b"Brazil", r.data)
            self.assertIn(b"Polymarket", r.data)  # honesty footer

    def test_api_json(self):
        m = Match(date="2026-06-15", team1="USA", team2="Wales")
        items = core.build_items(
            "2026-06-15", schedule=StubSchedule([m]), market_client=StubMarket({})
        )
        with mock.patch.object(self.webapp.core, "build_items", return_value=items):
            r = self.client.get("/api/day/2026-06-15")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["matches"][0]["has_pred"], False)


if __name__ == "__main__":
    unittest.main()
