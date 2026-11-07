from datetime import datetime, timezone

import pytest

from arb import windows
from arb.venues import kalshi, polymarket


def test_window_and_slug():
    now = datetime(2026, 11, 7, 0, 22, 31, tzinfo=timezone.utc)
    s, e = windows.current(now)
    assert s == datetime(2026, 11, 7, 0, 15, tzinfo=timezone.utc)
    assert e == datetime(2026, 11, 7, 0, 30, tzinfo=timezone.utc)
    assert windows.pm_event_slug("eth", s) == "eth-updown-15m-2026-11-07-0015z"


def test_kalshi_book_legacy_cents():
    js = {"orderbook": {"yes": [[45, 100], [44, 50]], "no": [[52, 80]]}}
    b = kalshi.parse_orderbook(js, 1.0)
    assert b.asks_up == [(0.48, 80)]                       # 1 - 0.52
    assert b.asks_down == [(0.55, 100), (0.56, 50)]        # 1 - 0.45, 1 - 0.44


def test_kalshi_book_dollars():
    js = {"orderbook_fp": {"yes_dollars": [["0.4500", "100.00"]], "no_dollars": [["0.0100", "5"]]}}
    b = kalshi.parse_orderbook(js, 1.0)
    assert b.asks_up == [(0.99, 5)] and b.asks_down == [(0.55, 100)]


def test_kalshi_result():
    assert kalshi.parse_result({"market": {"result": "yes"}}) == "up"
    assert kalshi.parse_result({"market": {"result": "no"}}) == "down"
    assert kalshi.parse_result({"market": {"result": ""}}) is None


def test_pm_book_variants():
    js = {"marketData": {"bids": [{"px": {"value": "0.40"}, "qty": "10"}],
                         "offers": [{"px": {"value": "0.43"}, "qty": "20"}]}}
    b = polymarket.parse_book(js, 1.0)
    assert b.asks_up == [(0.43, 20)] and b.asks_down == [(0.6, 10)]
    b2 = polymarket.parse_book(js, 1.0, book_is_up=False)
    assert b2.asks_down == [(0.43, 20)] and b2.asks_up == [(0.6, 10)]


def test_pm_result():
    assert polymarket.parse_result({"winner": "Up"}) == "up"
    assert polymarket.parse_result({"closed": True, "outcomes": '["Up","Down"]',
                                    "outcomePrices": '["0","1"]'}) == "down"
    assert polymarket.parse_result({"closed": False, "outcomes": ["Up", "Down"],
                                    "outcomePrices": ["1", "0"]}) is None
