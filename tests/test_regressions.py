"""Régressions observées en production (logs du 12 nov 2026).

1. book Polymarket intl : `/markets?slug=` masque les marchés clos -> résolution jamais trouvée,
   paper trades jamais réglés (colonnes payout / P&L / dis vides, fenêtres « complète = non ») ;
2. seuils journaliers XRP : le libellé Kalshi « $1.37991 or above » ne correspond à aucun seuil
   Polymarket -> `communs=0` ;
3. marchés journaliers US : `priceToBeat` null, le seuil est dans `strike.value` ;
4. météo : villes Kalshi absentes de la table + motif de rejet des stations peu explicite ;
5. délai d'apparition du book : jamais journalisé sans 404 (donc jamais en PM_FLAVOR=intl).
"""
import asyncio
import logging
from datetime import date, datetime, timezone

import pytest

from arb import main as m
from arb import windows
from arb.config import Config
from arb.daily_core import ks_threshold, ladder_note, match_pairs, thr_key
from arb.models import Book, MarketRef
from arb.venues import polymarket as pmus
from arb.venues.polymarket_intl import PolymarketIntlCrypto, parse_intl_result
from arb.weather_core import (CITIES, canon_city, classify_pair, day_end_utc, parse_ks_rain_market,
                              unrecognized_ks)

UTC = timezone.utc
START = datetime(2026, 11, 12, 19, 45, tzinfo=UTC)


# ---------- 1. résolution Polymarket intl ----------
class _Gamma:
    """Gamma : /markets ne renvoie le marché clos QUE si closed=true est demandé."""

    def __init__(self, market):
        self.market, self.calls = market, []

    async def _get(self, base, path, **params):
        self.calls.append((path, params))
        if path == "/markets":
            closed = params.get("closed") == "true"
            return [self.market] if self.market.get("closed") == closed else []
        return []


def _closed_market():
    return {"slug": PolymarketIntlCrypto.slug("btc", START), "closed": True,
            "umaResolutionStatus": "resolved", "outcomes": '["Up", "Down"]',
            "outcomePrices": '["1", "0"]'}


def test_fetch_market_finds_closed_market():
    c = PolymarketIntlCrypto(Config(), None)
    g = _Gamma(_closed_market())
    c._get = g._get
    got = asyncio.run(c.fetch_market(PolymarketIntlCrypto.slug("btc", START)))
    assert got is not None and got["closed"] is True
    assert {"slug": PolymarketIntlCrypto.slug("btc", START), "closed": "true"} in [p for _, p in g.calls]


def test_closed_window_resolves_up():
    """Le bug : fetch_event_result renvoyait None -> resolutions.complete=false pour toujours."""
    c = PolymarketIntlCrypto(Config(), None)
    c._get = _Gamma(_closed_market())._get
    r = asyncio.run(c.fetch_event_result("btc", START))
    assert r == {"result": "up", "strike": None, "settle": None, "status": "closed"}


def test_fetch_event_result_falls_back_to_event():
    c = PolymarketIntlCrypto(Config(), None)

    async def nothing(slug):
        return None

    async def events(**kw):
        return [{"markets": [_closed_market()]}]

    c.fetch_market, c.events = nothing, events
    assert asyncio.run(c.fetch_event_result("btc", START))["result"] == "up"


def test_result_accepted_when_uma_resolved_without_closed_flag():
    mk = _closed_market()
    mk.pop("closed")
    assert parse_intl_result(mk)["result"] == "up"
    assert parse_intl_result({**mk, "outcomePrices": '["0.5", "0.5"]'})["result"] is None


def test_open_market_still_found_first():
    c = PolymarketIntlCrypto(Config(), None)
    g = _Gamma({**_closed_market(), "closed": False, "outcomePrices": '["0.5", "0.5"]'})
    c._get = g._get
    assert asyncio.run(c.fetch_market("x")) is not None
    assert len(g.calls) == 1                      # pas d'appel inutile pour un marché ouvert


# ---------- 2. seuils journaliers ----------
def test_xrp_threshold_uses_floor_strike_not_label():
    kal = {"ticker": "KXXRPD-26NOV1217-T1.3799", "yes_sub_title": "$1.37991 or above",
           "floor_strike": 1.3799, "strike_type": "greater", "custom_strike": {"round_digits": "4"}}
    assert ks_threshold(kal) == pytest.approx(1.38)


def test_xrp_ladder_now_overlaps_polymarket():
    ks = [{"threshold": ks_threshold({"floor_strike": f, "strike_type": "greater",
                                      "yes_sub_title": f"${f}1 or above",
                                      "custom_strike": {"round_digits": "4"}})}
          for f in (0.8799, 0.8999, 0.9799, 0.9999, 1.0999, 1.1999)]
    pm = [{"threshold": t} for t in (0.85, 0.90, 0.95, 1.00, 1.05, 1.10, 1.20)]
    assert [th for th, _, _ in match_pairs(ks, pm)] == [0.90, 1.00, 1.10, 1.20]


def test_btc_eth_sol_thresholds_unchanged():
    assert ks_threshold({"yes_sub_title": "$83,250 or above", "floor_strike": 83249.99,
                         "strike_type": "greater", "custom_strike": {"round_digits": "2"}}) == 83250.0
    assert ks_threshold({"yes_sub_title": "$2,810 or above", "floor_strike": 2809.99,
                         "strike_type": "greater"}) == 2810.0
    assert ks_threshold({"floor_strike": 2250, "strike_type": "greater_or_equal"}) == 2250.0
    assert ks_threshold({"yes_sub_title": "$73,500 or above"}) == 73500.0
    assert ks_threshold({"title": "Will it rain?"}) is None


def test_round_digits_wins_over_floor_decimals():
    assert ks_threshold({"floor_strike": 1.4, "strike_type": "greater",
                         "custom_strike": {"round_digits": "4"}}) == pytest.approx(1.4001)


def test_ladder_note_explains_empty_overlap():
    ks = [{"threshold": 1250.0 + 40 * i} for i in range(40)]
    pm = [{"threshold": 1800.0 + 20 * i} for i in range(29)]
    assert match_pairs(ks, pm) == []
    note = ladder_note(ks, pm)
    assert "disjointes" in note and "pas=40" in note and "pas=20" in note
    assert "écart de seuil le plus proche = 10" in note


# ---------- 3. marchés journaliers US : seuil de référence ----------
def test_daily_strike_falls_back_to_strike_value():
    mk = {"status": "MARKET_STATUS_RESOLVED", "outcomes": '["Yes", "No"]', "outcomePrices": '["1", "0"]',
          "assetPriceTerms": {"priceToBeat": None, "strike": {"value": "66000", "currency": "USD"},
                              "settlementPrice": {"value": "74309.75", "currency": "USD"}}}
    got = pmus.parse_market(mk)
    assert (got["result"], got["strike"], got["settle"]) == ("up", 66000.0, 74309.75)


def test_price_to_beat_still_preferred():
    mk = {"assetPriceTerms": {"priceToBeat": {"value": "75996.63"}, "strike": {"value": "1"}}}
    assert pmus.parse_market(mk)["strike"] == 75996.63


# ---------- 4. météo ----------
NEW_CITIES = {"College Station": "CLL", "Columbus": "CMH", "Detroit": "DTW", "Newark": "EWR",
              "Indianapolis": "IND", "Lexington": "LEX", "Minneapolis": "MSP", "Milwaukee": "MKE",
              "Oklahoma City": "OKC", "Pittsburgh": "PIT", "Providence": "PVD", "San Antonio": "SAT",
              "Springfield": "SGF", "St. Louis": "STL", "Tampa": "TPA", "Trenton": "TTN",
              "West Palm Beach": "WPB"}


@pytest.mark.parametrize("name,key", sorted(NEW_CITIES.items()))
def test_new_cities_recognized(name, key):
    assert canon_city(name) == key
    assert canon_city(f"{name}, TX") == key
    assert unrecognized_ks({"ticker": f"KXRAIN-26NOV13-{key}", "yes_sub_title": name}) is None


def test_kalshi_ticker_suffixes_map_to_city():
    for suffix, key in (("MIN", "MSP"), ("SATX", "SAT"), ("WPB", "WPB"), ("CLL", "CLL")):
        info = parse_ks_rain_market({"ticker": f"KXRAIN-26NOV13-{suffix}", "yes_sub_title": "?",
                                     "rules_primary": "precipitation at CLIXXX"})
        assert info["city"] == key and info["day"] == date(2026, 11, 13)


def test_new_city_offsets_and_day_end():
    assert CITIES["MSP"][1] == -6 and CITIES["DTW"][1] == -5
    assert day_end_utc("MSP", date(2026, 11, 13)) == datetime(2026, 11, 14, 6, tzinfo=UTC)
    assert day_end_utc("PIT", date(2026, 11, 13)) == datetime(2026, 11, 14, 5, tzinfo=UTC)


def test_station_mismatch_reason_names_stations():
    ks = {"city": "HOU", "rules": "total precipitation at CLIHOU in Houston", "codes": {"HOU"}}
    pm = {"city": "HOU", "rules": "NWS climate day for KIAH", "codes": {"IAH"}}
    got = classify_pair(ks, pm, {"HOU"})
    assert not got["tradable"] and got["station_status"] == "mismatch"
    assert got["reason"] == "stations différentes : kalshi=HOU pm=IAH"


def test_springfield_requires_station_code():
    from arb.weather import trusted_set
    assert "SGF" not in trusted_set(Config())          # nom partagé par plusieurs États
    assert "MSP" in trusted_set(Config())


# ---------- 5. délai d'apparition du book ----------
def _book(venue):
    import time
    return Book(venue, asks_up=[(0.40, 100)], asks_down=[(0.50, 100)], ts_recv=time.time())


def test_first_book_logged_once_even_without_404(monkeypatch, caplog):
    cfg, state = Config(), m.State(Config())

    async def noop(*a, **k):
        return None

    async def wid(*a, **k):
        return 1

    async def has_trade(*a, **k):
        return True

    monkeypatch.setattr(m.db, "get_or_create_window", wid)
    monkeypatch.setattr(m.db, "insert_quote", noop)
    monkeypatch.setattr(m.db, "insert_signal", noop)
    monkeypatch.setattr(m.db, "has_trade", has_trade)

    class _Venue:
        def __init__(self, venue):
            self.venue = venue

        async def discover(self, asset, start, end):
            return MarketRef(self.venue, "ID", slug="slug")

        async def fetch_book(self, ref):
            return _book(self.venue)

    kal, pmc = _Venue("kalshi"), _Venue("polymarket")
    with caplog.at_level(logging.INFO, logger="arb"):
        for _ in range(3):
            asyncio.run(m.tick_asset("btc", cfg, None, kal, pmc, state.known, state.dis_cache, state))
    hits = [r for r in caplog.records if "polymarket book disponible" in r.getMessage()]
    assert len(hits) == 1 and "après 0 x 404" in hits[0].getMessage()
    assert state.pm_book_seen and not state.pm_404
    state.reset_caches()
    assert not state.pm_book_seen


def test_book_seen_pruned_on_new_window():
    state = m.State(Config())
    old = datetime(2026, 11, 12, 19, 0, tzinfo=UTC)
    state.pm_book_seen.add(("btc", old))
    state.pm_404[("btc", old)] = 2
    start, end = windows.current(datetime.now(UTC))
    assert start > old
    for k in [k for k in state.pm_book_seen if k[1] < start]:
        state.pm_book_seen.discard(k)
    assert not state.pm_book_seen
