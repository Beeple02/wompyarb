from datetime import date, datetime, timezone

import pytest

from arb.config import Config
from arb.daily_core import evaluate_pair
from arb.models import Book
from arb.venues.polymarket_intl import parse_intl_result
from arb.weather import backoff_delay, trusted_set
from arb.weather_core import (canon_city, classify_pair, day_end_utc, directions_for, local_std_today,
                              parse_intl_rain_event, parse_ks_rain_event, parse_ks_rain_market,
                              parse_rain_question, station_codes, trace_class)

UTC = timezone.utc


def test_canon_city():
    assert canon_city("Chicago, IL") == "CHI"
    assert canon_city("New York City") == "NYC"
    assert canon_city("Los Angeles, CA") == "LAX"
    assert canon_city("San Francisco") == "SFO"
    assert canon_city("Atlantis") is None


def test_kalshi_ticker_and_market():
    assert parse_ks_rain_event("KXRAIN-26NOV08-NYC") == date(2026, 11, 8)
    assert parse_ks_rain_event("KXRAINNYC-26JUN08") is None
    m = parse_ks_rain_market({"ticker": "KXRAIN-26NOV08-MIA", "event_ticker": "KXRAIN-26NOV08",
                              "yes_sub_title": "Miami", "rules_primary":
                              "If the total precipitation at CLIMIA in Miami in Nov 8, 2026 is strictly greater than 0 inches"})
    assert m["city"] == "MIA" and m["day"] == date(2026, 11, 8) and m["codes"] == {"MIA"}
    assert parse_ks_rain_market({"ticker": "KXRAIN-26NOV08-ZZZ"}) is None


def test_rain_question_year_from_end_date():
    end = datetime(2026, 8, 23, 4, 0, tzinfo=UTC)
    assert parse_rain_question("Will it rain in Chicago, IL on August 22?", end) == ("CHI", date(2026, 8, 22))
    assert parse_rain_question("Will it snow in Chicago on August 22?", end) is None


def test_trace_class_three_wordings():
    assert trace_class('Measurable precipitation is 0.01 inches or more. A trace ("T") does not count.') == "no"
    assert trace_class("Trace amounts (T) and missing values are counted as 0 inches.") == "no"
    assert trace_class("If the Expiration Value is T (Trace), the market resolves to Yes.") == "yes"
    assert trace_class("It rains or it does not.") == "unknown"


def test_station_codes():
    assert station_codes("total precipitation at CLINYC in New York City") == {"NYC"}
    assert station_codes("see site=OKX&product=CLI&issuedby=NYC (KNYC)") == {"NYC"}
    assert station_codes("no codes here") == set()


def _ks(codes, rules):
    return {"city": "MIA", "day": date(2026, 11, 8), "ticker": "T", "codes": codes, "rules": rules}


def _pm(codes, rules):
    return {"city": "MIA", "day": date(2026, 11, 8), "slug": "s", "codes": codes, "rules": rules}


KS_TRACE_YES = "T (Trace) resolves to Yes"
KS_TRACE_NO = "Trace (T) is counted as 0 inches"
PM_TRACE_NO = 'A trace ("T") does not count.'


def test_classify_directional_rule():
    # Kalshi : trace = Yes ; Polymarket : trace = No -> seule direction sûre : Yes Kalshi + No Polymarket
    c = classify_pair(_ks({"MIA"}, KS_TRACE_YES), _pm({"MIA"}, PM_TRACE_NO), set())
    assert c["tradable"] and c["safe_direction"] == "PD_KU" and c["station_status"] == "match"
    # mêmes règles des deux côtés : les deux directions sont sûres
    c = classify_pair(_ks({"MIA"}, KS_TRACE_NO), _pm({"MIA"}, PM_TRACE_NO), set())
    assert c["safe_direction"] == "BOTH"
    # Kalshi plus strict que Polymarket -> Yes Polymarket + No Kalshi
    c = classify_pair(_ks({"MIA"}, KS_TRACE_NO), _pm({"MIA"}, "trace T resolves to Yes"), set())
    assert c["safe_direction"] == "PU_KD"


def test_classify_blocks_unverified():
    c = classify_pair(_ks({"MDW"}, KS_TRACE_NO), _pm({"ORD"}, PM_TRACE_NO), set())
    assert not c["tradable"] and c["station_status"] == "mismatch"
    c = classify_pair(_ks({"MIA"}, KS_TRACE_NO), _pm(set(), PM_TRACE_NO), set())
    assert not c["tradable"] and c["station_status"] == "assumed"
    c = classify_pair(_ks({"MIA"}, KS_TRACE_NO), _pm(set(), PM_TRACE_NO), {"MIA"})
    assert c["tradable"]                                    # ville explicitement approuvée
    c = classify_pair(_ks({"MIA"}, "no info"), _pm({"MIA"}, PM_TRACE_NO), set())
    assert not c["tradable"] and "traces" in c["reason"]


def test_directions_and_trusted():
    assert list(directions_for("PD_KU")) == ["PD_KU"]
    assert set(directions_for("BOTH")) == {"PU_KD", "PD_KU"} and set(directions_for(None)) == {"PU_KD", "PD_KU"}
    assert trusted_set(Config(weather_trusted_cities="mia, nyc ,")) == {"MIA", "NYC"}


def test_day_end_uses_standard_time():
    assert day_end_utc("NYC", date(2026, 11, 7)) == datetime(2026, 11, 8, 5, 0, tzinfo=UTC)
    assert day_end_utc("LAX", date(2026, 7, 1)) == datetime(2026, 7, 2, 8, 0, tzinfo=UTC)   # pas d'heure d'été
    assert local_std_today("NYC", datetime(2026, 11, 8, 4, 0, tzinfo=UTC)) == date(2026, 11, 7)


# Structure SYNTHÉTIQUE d'un événement Gamma (champs documentés : outcomes/clobTokenIds en chaînes JSON).
GAMMA_EVENT = {"slug": "where-will-it-rain-on-november-8", "markets": [
    {"slug": "will-it-rain-in-miami-on-november-8", "question": "Will it rain in Miami, FL on November 8?",
     "endDate": "2026-11-09T05:00:00Z", "outcomes": "[\"Yes\",\"No\"]", "clobTokenIds": "[\"111\",\"222\"]",
     "description": 'Measurable precipitation... issuedby=MIA. A trace ("T") does not count.'},
    {"slug": "other", "question": "Will it snow in Miami on November 8?", "outcomes": "[\"Yes\",\"No\"]",
     "clobTokenIds": "[\"1\",\"2\"]"}]}


def test_parse_intl_rain_event():
    r = parse_intl_rain_event(GAMMA_EVENT)
    assert len(r) == 1 and r[0]["city"] == "MIA" and r[0]["day"] == date(2026, 11, 8)
    assert r[0]["token_yes"] == "111" and r[0]["codes"] == {"MIA"}
    assert trace_class(r[0]["rules"]) == "no"


def test_parse_intl_result():
    done = {"closed": True, "outcomes": "[\"Yes\",\"No\"]", "outcomePrices": "[\"0\",\"1\"]"}
    assert parse_intl_result(done)["result"] == "down"
    assert parse_intl_result({**done, "outcomePrices": "[\"1\",\"0\"]"})["result"] == "up"
    assert parse_intl_result({**done, "closed": False})["result"] is None


def test_evaluate_pair_with_restricted_direction():
    cfg = Config(pm_fee_coef=0.0, pm_intl_fee_coef=0.0, ks_fee_coef=0.0, max_stale_s=10, max_skew_s=10)
    now = 1000.0
    pm = Book("polymarket", [(0.05, 100)], [(0.90, 100)], ts_recv=now)     # Yes 0.05 / No 0.90
    ks = Book("kalshi", [(0.90, 100)], [(0.08, 100)], ts_recv=now)         # Yes 0.90 / No 0.08
    both = evaluate_pair(pm, ks, 10, 5000, now, cfg, min_edge_bps=100, no_entry_last_s=0)
    only = evaluate_pair(pm, ks, 10, 5000, now, cfg, directions=directions_for("PD_KU"),
                         min_edge_bps=100, no_entry_last_s=0)   # PD_KU = No PM 0.90 + Yes KS 0.90 : pas d'arb
    assert both.direction == "PU_KD" and both.net == pytest.approx(1 - 0.05 - 0.08)
    assert only.direction == "PD_KU" and only.status == "OBSERVE" and only.net < 0
    late = evaluate_pair(pm, ks, 10, -5, now, cfg, min_edge_bps=100, no_entry_last_s=0)
    assert late.status == "OBSERVE"                                        # plus d'entrée après la fin du jour


def test_weather_backoff():
    assert [backoff_delay(i) for i in range(5)] == [300, 600, 1200, 2400, 3600]
