import json

from arb.venues import kalshi, polymarket

# Extrait réel (réduit) de GET /v1/events/slug/btc-updown-15m-2026-11-07-0945z, observé le 2026-11-07.
BTC_EVENT = {"event": {"slug": "btc-updown-15m-2026-11-07-0945z", "closed": True, "ended": True, "markets": [{
    "slug": "cpc-btc-updown-15m-2026-11-07-0945z", "closed": True,
    "outcomes": "[\"Yes\",\"No\"]", "outcomePrices": "[\"0\",\"1\"]",
    "status": "MARKET_STATUS_RESOLVED", "feeCoefficient": 0.095,
    "marketSides": [{"description": "Yes", "long": True}, {"description": "No", "long": False}],
    "assetPriceTerms": {"indexSymbol": "BRTI", "priceToBeat": {"value": "75996.63", "currency": "USD"},
                        "settlementPrice": {"value": "75956.52", "currency": "USD"}}}]}}


def test_pm_event_resolved_down():
    r = polymarket.parse_event(BTC_EVENT)
    assert r["result"] == "down" and r["status"] == "MARKET_STATUS_RESOLVED"
    assert r["strike"] == 75996.63 and r["settle"] == 75956.52


def test_pm_event_resolved_up():
    ev = json.loads(json.dumps(BTC_EVENT))
    ev["event"]["markets"][0]["outcomePrices"] = "[\"1\",\"0\"]"
    assert polymarket.parse_event(ev)["result"] == "up"


def test_pm_event_not_resolved_yet():
    ev = json.loads(json.dumps(BTC_EVENT))
    m = ev["event"]["markets"][0]
    m["status"], m["outcomePrices"] = "MARKET_STATUS_OPEN", "[\"0.5\",\"0.5\"]"
    m["assetPriceTerms"]["settlementPrice"] = None
    r = polymarket.parse_event(ev)
    assert r["result"] is None and r["settle"] is None and r["strike"] == 75996.63


def test_pm_event_missing_markets():
    assert polymarket.parse_event({"event": {"markets": []}}) is None
    assert polymarket.parse_event({}) is None


def test_kalshi_market_result_full():
    js = {"market": {"result": "no", "floor_strike": 75996.63, "expiration_value": "75956.52"}}
    assert kalshi.parse_market_result(js) == {"result": "down", "strike": 75996.63, "settle": 75956.52}
    assert kalshi.parse_market_result({"market": {"result": ""}})["result"] is None


def test_backoff_and_implied():
    from arb.main import backoff_delay, implied_result
    assert [backoff_delay(i) for i in range(7)] == [20, 40, 80, 160, 320, 600, 600]
    assert implied_result(100.0, 100.0) == "up"      # égalité -> Up
    assert implied_result(100.0, 99.99) == "down"
    assert implied_result(None, 1.0) is None
