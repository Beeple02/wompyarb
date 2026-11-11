import asyncio
from datetime import date, datetime, timezone

from arb.config import Config
from arb.main import make_pm
from arb.venues.polymarket import PolymarketClient
from arb.venues.polymarket_intl import PolymarketIntlCrypto
from arb.weather import gather_weather, trusted_set
from arb.weather_core import unrecognized_intl, unrecognized_ks

UTC = timezone.utc


def test_flavor_switch_and_fee():
    assert isinstance(make_pm(Config(), None), PolymarketIntlCrypto)          # international par défaut
    assert isinstance(make_pm(Config(pm_flavor="us"), None), PolymarketClient)
    assert Config().pm_fee_eff == Config().pm_intl_fee_coef
    assert Config(pm_flavor="us").pm_fee_eff == Config().pm_fee_coef


def test_intl_slug_is_window_start_epoch():
    start = datetime(2026, 4, 17, 19, 15, tzinfo=UTC)                         # 15h15 ET
    assert PolymarketIntlCrypto.slug("btc", start) == "btc-updown-15m-1776453300"


def test_intl_discover_picks_up_token_and_result():
    c = PolymarketIntlCrypto(Config(), None)

    async def events(**kw):
        assert kw["slug"] == "eth-updown-15m-1776453300"
        return [{"markets": [{"slug": "eth-updown-15m-1776453300", "outcomes": "[\"Up\",\"Down\"]",
                              "clobTokenIds": "[\"TOK_UP\",\"TOK_DOWN\"]"}]}]

    async def fetch_market(slug):
        return {"closed": True, "outcomes": "[\"Up\",\"Down\"]", "outcomePrices": "[\"0\",\"1\"]"}

    c.events, c.fetch_market = events, fetch_market
    start = datetime(2026, 4, 17, 19, 15, tzinfo=UTC)
    ref = asyncio.run(c.discover("eth", start, start))
    assert ref.ident == "TOK_UP" and ref.slug == "eth-updown-15m-1776453300"
    assert asyncio.run(c.fetch_event_result("eth", start))["result"] == "down"


def test_trusted_auto_all_explicit():
    assert "MIA" in trusted_set(Config()) and "CHI" not in trusted_set(Config())       # auto : sans villes ambiguës
    assert "CHI" in trusted_set(Config(weather_trusted_cities="all"))
    assert trusted_set(Config(weather_trusted_cities="")) == set()


def test_unrecognized_names_are_reported():
    assert unrecognized_ks({"ticker": "KXRAIN-26NOV08-ZZZ", "yes_sub_title": "Zanzibar"}) == "Zanzibar"
    assert unrecognized_ks({"ticker": "KXRAIN-26NOV08-MIA", "yes_sub_title": "Miami"}) is None
    ev = {"markets": [{"question": "Will it rain in Zanzibar on November 8?"},
                      {"question": "Will it rain in Miami, FL on November 8?"}]}
    assert unrecognized_intl(ev) == ["Zanzibar"]


class _FakeKal:
    async def list_markets(self, series):
        base = "T (Trace) resolves to Yes"
        return [
            {"ticker": "KXRAIN-26NOV08-MIA", "event_ticker": "KXRAIN-26NOV08", "yes_sub_title": "Miami",
             "rules_primary": f"total precipitation at CLIMIA in Miami. {base}"},
            {"ticker": "KXRAIN-26NOV08-NYC", "event_ticker": "KXRAIN-26NOV08", "yes_sub_title": "New York City",
             "rules_primary": "precipitation at CLINYC. Trace (T) is counted as 0 inches"},
            {"ticker": "KXRAIN-26NOV08-SEA", "event_ticker": "KXRAIN-26NOV08", "yes_sub_title": "Seattle",
             "rules_primary": "precipitation at CLISEA. Trace (T) is counted as 0 inches"},
            {"ticker": "KXRAIN-26NOV08-ZZZ", "event_ticker": "KXRAIN-26NOV08", "yes_sub_title": "Zanzibar"},
        ]


class _FakePm:
    async def weather_events(self, max_pages=6):
        def mk(city, extra=""):
            return {"slug": f"rain-{city.lower()}", "question": f"Will it rain in {city} on November 8?",
                    "endDate": "2026-11-09T06:00:00Z", "outcomes": "[\"Yes\",\"No\"]", "clobTokenIds": "[\"1\",\"2\"]",
                    "description": 'A trace ("T") does not count. ' + extra}
        return [{"slug": "e1", "markets": [mk("Miami, FL", "issuedby=MIA"), mk("New York City"),
                                           mk("Chicago, IL"), mk("Atlantis")]}]

    async def search_events(self, q):
        return []


def test_scanner_compares_cities_and_matches():
    now = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)
    res = asyncio.run(gather_weather(Config(), _FakeKal(), _FakePm(), now))
    by_city = {p["city"]: p for p in res["pairs"]}
    assert set(by_city) == {"MIA", "NYC"}                                    # villes présentes des deux côtés
    assert by_city["MIA"]["tradable"] and by_city["MIA"]["safe_direction"] == "PD_KU"      # station vérifiée
    assert not by_city["NYC"]["tradable"] and by_city["NYC"]["station_status"] == "assumed"   # ambiguë : pas de code PM
    assert res["only_ks"] == ["SEA@2026-11-08"] and res["only_pm"] == ["CHI@2026-11-08"]
    assert res["unknown_ks"] == ["Zanzibar"] and res["unknown_pm"] == ["Atlantis"]
    assert res["cities"]["MIA"]["common_days"] == ["2026-11-08"] and res["cities"]["SEA"]["common_days"] == []
