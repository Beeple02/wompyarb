"""Marchés « Where will it rain ? » par ville : parsing, appariement et règles de sécurité (fonctions pures).

Risques d'équivalence entre Kalshi (KXRAIN) et Polymarket, vérifiés AVANT tout trade :
  * même ville ET même station NWS (ex. Chicago : O'Hare ≠ Midway) ;
  * même jour climatologique (minuit à minuit, heure LOCALE STANDARD) ;
  * traitement d'une trace de pluie (« T ») : Kalshi l'a rédigé de deux façons (Yes / compté comme 0),
    Polymarket : « une trace ne compte pas ». Quand les deux règles diffèrent, seule une direction paie >= 1 :
    Yes sur la venue où la trace compte + No sur l'autre (paie 2 les jours de trace, 1 sinon).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from .daily_core import parse_iso
from .venues.util import find_key

# Clé canonique -> (alias minuscules, décalage UTC de l'heure STANDARD locale)
CITIES: dict[str, tuple[tuple[str, ...], int]] = {
    "NYC": (("new york city", "new york", "nyc", "central park", "ny"), -5),
    "CHI": (("chicago", "chi"), -6),
    "MIA": (("miami", "mia"), -5),
    "PHL": (("philadelphia", "philly", "phil", "phl"), -5),
    "LAX": (("los angeles", "lax", "la"), -8),
    "SFO": (("san francisco", "sfo", "sf"), -8),
    "SEA": (("seattle", "sea"), -8),
    "BOS": (("boston", "bos"), -5),
    "DEN": (("denver", "den"), -7),
    "ABQ": (("albuquerque", "abq"), -7),
    "DFW": (("dallas", "dal", "dfw"), -6),
    "HOU": (("houston", "hou"), -6),
    "AUS": (("austin", "aus"), -6),
    "MSY": (("new orleans", "msy", "no"), -6),
    "DCA": (("washington", "dca"), -5),
    "ATL": (("atlanta", "atl"), -5),
    "PHX": (("phoenix", "phx"), -7),
    "LAS": (("las vegas", "las"), -8),
}
# Villes où plusieurs stations NWS coexistent : l'appariement exige une station vérifiée par code.
AMBIGUOUS_CITIES = {"CHI", "NYC", "DFW", "HOU", "LAX", "SFO", "DCA"}

_MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
_MONTH_NAMES = {n: i for i, n in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
_KS_EVT = re.compile(r"KXRAIN-(\d{2})([A-Z]{3})(\d{2})(?:-|$)")
_Q_RAIN = re.compile(r"rain in (.+?) on ([A-Za-z]+)\.? (\d{1,2})", re.I)
_T_YES = re.compile(r"(?:trace|\(T\)|\bT\b)[^.]{0,120}?(?:resolves?|resolved|settles?)[^.]{0,30}?['\"“]?yes", re.I)
_T_NO = re.compile(r"(?:trace|\(T\))[^.]{0,120}?(?:does not|do not|will not|not qualify|not count|counted? as (?:0|zero)|"
                   r"counts? as (?:0|zero)|treated as (?:0|zero))", re.I)
_CLI = re.compile(r"\bCLI([A-Z]{3})\b")
_ISSUEDBY = re.compile(r"issuedby=([A-Za-z]{3})\b")
_ICAO = re.compile(r"\bK([A-Z]{3})\b")


def canon_city(text: str) -> str | None:
    """'Chicago, IL' / 'New York City' / 'NYC' -> clé canonique (alias le plus long contenu dans le texte)."""
    t = re.sub(r"[^a-z ]", " ", (text or "").lower())
    t = " " + re.sub(r"\s+", " ", t).strip() + " "
    best, best_len = None, 0
    for key, (aliases, _) in CITIES.items():
        for a in aliases:
            if f" {a} " in t and len(a) > best_len:
                best, best_len = key, len(a)
    return best


def std_offset_hours(city: str) -> int:
    return CITIES[city][1]


def day_end_utc(city: str, day: date) -> datetime:
    """Fin du jour climatologique : 24:00 heure locale STANDARD (pas d'heure d'été), en UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1) \
        - timedelta(hours=std_offset_hours(city))


def local_std_today(city: str, now: datetime) -> date:
    return (now.astimezone(timezone.utc) + timedelta(hours=std_offset_hours(city))).date()


def parse_ks_rain_event(ticker: str) -> date | None:
    """KXRAIN-26NOV08-NYC -> 2026-11-08."""
    m = _KS_EVT.search((ticker or "").upper())
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def parse_rain_question(q: str, end: datetime | None = None) -> tuple[str, date] | None:
    """'Will it rain in Chicago, IL on August 22?' -> ('CHI', date). L'année vient de la date de fin."""
    m = _Q_RAIN.search(q or "")
    if not m:
        return None
    city = canon_city(m.group(1))
    mon = _MONTH_NAMES.get(m.group(2).lower())
    if city is None or mon is None:
        return None
    year = (end or datetime.now(timezone.utc)).year
    try:
        d = date(year, mon, int(m.group(3)))
    except ValueError:
        return None
    if end is not None and d > end.date() + timedelta(days=200):
        d = date(year - 1, mon, d.day)
    return city, d


def trace_class(text: str) -> str:
    """'yes' (une trace fait gagner Yes), 'no' (une trace ne compte pas), 'unknown'."""
    if _T_YES.search(text or ""):
        return "yes"
    if _T_NO.search(text or ""):
        return "no"
    return "unknown"


def station_codes(text: str) -> set[str]:
    t = text or ""
    return ({m.upper() for m in _CLI.findall(t)} | {m.upper() for m in _ISSUEDBY.findall(t)}
            | {m.upper() for m in _ICAO.findall(t)})


def parse_ks_rain_market(m: dict) -> dict | None:
    ticker = str(m.get("ticker") or "")
    day = parse_ks_rain_event(m.get("event_ticker") or ticker)
    city = (canon_city(str(m.get("yes_sub_title") or "")) or canon_city(str(m.get("subtitle") or ""))
            or canon_city(ticker.rsplit("-", 1)[-1]))
    if not ticker or day is None or city is None:
        return None
    rules = " ".join(str(m.get(k) or "") for k in ("rules_primary", "rules_secondary"))
    return {"ticker": ticker, "city": city, "day": day, "rules": rules, "codes": station_codes(rules)}


def _json_list(v):
    import json
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def parse_intl_rain_event(ev: dict) -> list[dict]:
    """Événement Gamma -> marchés « pluie » exploitables : [{slug, city, day, token_yes, rules, codes, ...}]."""
    out = []
    for m in (ev.get("markets") or []):
        if not isinstance(m, dict):
            continue
        end = parse_iso(m.get("endDate")) or parse_iso(ev.get("endDate"))
        parsed = parse_rain_question(str(m.get("question") or ""), end)
        if parsed is None:
            continue
        outcomes, tokens = _json_list(m.get("outcomes")), _json_list(m.get("clobTokenIds"))
        if not outcomes or not tokens or len(outcomes) != len(tokens):
            continue
        idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), None)
        if idx is None:
            continue
        rules = " ".join(str(x or "") for x in (m.get("description"), m.get("resolutionSource"),
                                                 ev.get("description"), ev.get("resolutionSource")))
        out.append({"slug": m.get("slug"), "event_slug": ev.get("slug"), "city": parsed[0], "day": parsed[1],
                    "token_yes": str(tokens[idx]), "rules": rules, "codes": station_codes(rules),
                    "closed": bool(m.get("closed"))})
    return out


def unrecognized_ks(m: dict) -> str | None:
    """Libellé d'un marché Kalshi KXRAIN dont la ville n'est pas dans la table (pour le signaler, pas l'ignorer)."""
    ticker = str(m.get("ticker") or "")
    if parse_ks_rain_event(m.get("event_ticker") or ticker) is None or parse_ks_rain_market(m) is not None:
        return None
    return str(m.get("yes_sub_title") or m.get("subtitle") or ticker.rsplit("-", 1)[-1] or "?")


def unrecognized_intl(ev: dict) -> list[str]:
    """Villes de questions « Will it rain in X on … » absentes de la table."""
    out = []
    for m in (ev.get("markets") or []):
        mt = _Q_RAIN.search(str((m or {}).get("question") or ""))
        if mt and canon_city(mt.group(1)) is None:
            out.append(mt.group(1).strip())
    return out


def classify_pair(ks: dict, pm: dict, trusted: set[str]) -> dict:
    """Décide si le couple est négociable et dans quelle(s) direction(s). `ks`/`pm` : sorties des parseurs."""
    city = ks["city"]
    ks_tr, pm_tr = trace_class(ks["rules"]), trace_class(pm["rules"])
    if ks["codes"] and pm["codes"]:
        station = "match" if ks["codes"] & pm["codes"] else "mismatch"
    else:
        station = "assumed"
    reason, safe = "", None
    if station == "mismatch":
        reason = "stations différentes"
    elif station == "assumed" and city not in trusted:
        reason = "station non vérifiable (ville absente de WEATHER_TRUSTED_CITIES)"
    if not reason:
        if ks_tr == "unknown" or pm_tr == "unknown":
            reason = "règle des traces illisible"
        elif ks_tr == pm_tr:
            safe = "BOTH"
        elif ks_tr == "yes":      # trace = Yes chez Kalshi, No chez Polymarket
            safe = "PD_KU"        # Yes Kalshi + No Polymarket : paie >= 1
        else:
            safe = "PU_KD"        # Yes Polymarket + No Kalshi : paie >= 1
    return {"station_status": station, "ks_trace": ks_tr, "pm_trace": pm_tr, "safe_direction": safe,
            "tradable": bool(safe), "reason": reason, "ambiguous": city in AMBIGUOUS_CITIES,
            "ks_station": ",".join(sorted(ks["codes"])), "pm_station": ",".join(sorted(pm["codes"]))}


def directions_for(safe: str | None) -> dict:
    from .engine import DIRECTIONS
    if safe in DIRECTIONS:
        return {safe: DIRECTIONS[safe]}
    return dict(DIRECTIONS)
