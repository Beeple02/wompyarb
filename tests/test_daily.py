from datetime import date, datetime, timezone

import pytest

from arb.config import Config
from arb.daily import backoff_delay
from arb.daily_core import (evaluate_pair, event_of, ks_mid, ks_px, ks_threshold, match_pairs,
                            parse_ks_event_ticker, parse_pm_daily_event, parse_threshold_text,
                            pm_daily_slug, pm_market_threshold, pm_settle_from_date)
from arb.models import Book

UTC = timezone.utc


def test_settle_times_est_and_edt():
    # 5 pm New York : 22:00Z en hiver (EST), 21:00Z en été (EDT)
    assert parse_ks_event_ticker("KXBTCD-26NOV0817") == datetime(2026, 11, 8, 22, 0, tzinfo=UTC)
    assert parse_ks_event_ticker("KXBTCD-26AUG2617") == datetime(2026, 8, 26, 21, 0, tzinfo=UTC)
    assert parse_ks_event_ticker("KXBTCD-26NOV0817-T73499.99") == datetime(2026, 11, 8, 22, 0, tzinfo=UTC)
    assert parse_ks_event_ticker("KXBTC15M-26NOV071730") is None
    assert pm_settle_from_date(date(2026, 11, 8)) == datetime(2026, 11, 8, 22, 0, tzinfo=UTC)
    assert pm_daily_slug("btc", date(2026, 11, 8)) == "btc-above-day-2026-11-08"


def test_event_of():
    assert event_of("KXBTCD-26NOV0817-T73499.99") == "KXBTCD-26NOV0817"
    assert event_of("KXBTCD-26NOV0817") == "KXBTCD-26NOV0817"


def test_threshold_text():
    assert parse_threshold_text("$73,500 or above") == 73500.0
    assert parse_threshold_text("$1.15 or above") == 1.15
    assert parse_threshold_text("settle to Yes if ... is at or above $2,250 at 5:00PM ET") == 2250.0
    assert parse_threshold_text("between 1 and 2") is None


def test_kalshi_threshold_label_and_floor_fallback():
    assert ks_threshold({"yes_sub_title": "$73,500 or above"}) == 73500.0
    assert ks_threshold({"floor_strike": 73499.99, "strike_type": "greater"}) == 73500.0
    assert ks_threshold({"floor_strike": 1.1499, "strike_type": "greater"}) == pytest.approx(1.15)
    assert ks_threshold({"floor_strike": 2250, "strike_type": "greater_or_equal"}) == 2250.0
    assert ks_threshold({"title": "Will it rain?"}) is None


# Structure SYNTHÉTIQUE (le JSON réel d'un événement above-day n'a pas encore été observé).
PM_EVENT = {"event": {"slug": "btc-above-day-2026-11-08", "endDate": "2026-11-08T22:00:00Z", "markets": [
    {"slug": "m-73500", "title": "$73,500 or above", "status": "MARKET_STATUS_OPEN"},
    {"slug": "m-73750", "description": "settle to Yes if the price of Bitcoin is at or above $73,750 at 5:00PM ET"},
    {"slug": "m-bad", "title": "Something else"}]}}


def test_parse_pm_daily_event():
    r = parse_pm_daily_event(PM_EVENT)
    assert r["settle"] == datetime(2026, 11, 8, 22, 0, tzinfo=UTC)
    assert [m["threshold"] for m in r["markets"]] == [73500.0, 73750.0]
    assert set(r["by_slug"]) == {"m-73500", "m-73750", "m-bad"}
    assert pm_market_threshold({"assetPriceTerms": {"strike": {"value": "74000"}}}) == 74000.0
    assert parse_pm_daily_event({"event": {"markets": []}}) is None


def test_match_pairs_only_common_thresholds():
    ks = [{"ticker": "K1", "threshold": 73500.0}, {"ticker": "K2", "threshold": 73750.0},
          {"ticker": "K3", "threshold": 74000.0}]
    pm = [{"slug": "P1", "threshold": 73500.0}, {"slug": "P3", "threshold": 74000.0},
          {"slug": "P4", "threshold": 74250.0}]
    got = [(t, k["ticker"], p["slug"]) for t, k, p in match_pairs(ks, pm)]
    assert got == [(73500.0, "K1", "P1"), (74000.0, "K3", "P3")]


def test_ks_px_and_mid():
    assert ks_px({"yes_bid": 55}, "yes_bid") == 0.55
    assert ks_px({"yes_bid_dollars": "0.5600"}, "yes_bid") == 0.56
    assert ks_px({"yes_bid": 0}, "yes_bid") is None
    assert ks_mid({"yes_bid": 40, "yes_ask": 44}) == pytest.approx(0.42)
    assert ks_mid({"yes_ask": 44}) == 0.44
    assert ks_mid({}) is None


NOW = 1_000.0


def _book(venue, up, down, ts=NOW):
    return Book(venue, asks_up=up, asks_down=down, ts_recv=ts)


def _cfg():
    return Config(pm_fee_coef=0.0, pm_intl_fee_coef=0.0, ks_fee_coef=0.0)


def test_evaluate_pair_actionable_and_regimes():
    pm = _book("polymarket", [(0.45, 100)], [(0.58, 100)])
    ks = _book("kalshi", [(0.57, 100)], [(0.50, 100)])
    ev = evaluate_pair(pm, ks, 10, 5000, NOW, _cfg())          # PU_KD : 0.45 + 0.50 = 0.95
    assert ev.direction == "PU_KD" and ev.status == "ACTIONABLE" and ev.net == pytest.approx(0.05)
    ev = evaluate_pair(pm, ks, 10, 100, NOW, _cfg())            # trop près du règlement
    assert ev.status == "OBSERVE" and ev.reason == "last_minutes_regime"
    assert evaluate_pair(_book("polymarket", [(0.45, 2)], [(0.5, 2)]), ks, 10, 5000, NOW,
                         _cfg()).status == "REJECT_DEPTH"
    stale = _book("polymarket", [(0.45, 100)], [(0.58, 100)], ts=NOW - 5)
    assert evaluate_pair(stale, ks, 10, 5000, NOW, _cfg()).status == "REJECT_STALE"


def test_daily_backoff():
    assert [backoff_delay(i) for i in range(7)] == [60, 120, 240, 480, 960, 1800, 1800]
