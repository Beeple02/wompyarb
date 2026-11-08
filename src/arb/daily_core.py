"""Marchés journaliers « above/below » : parsing, appariement des seuils et évaluation (fonctions pures).

Kalshi (séries KXBTCD, KXETHD, KXSOLD, KXXRPD) et Polymarket US (événements `{asset}-above-day-AAAA-MM-JJ`)
règlent tous deux sur la moyenne de 60 prix de l'indice CF Benchmarks dans la dernière minute avant
17:00 ET. « $X or above » = Yes. Un seuil identique + une heure de règlement identique = même événement.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .config import Config
from .engine import DIRECTIONS, Evaluation, kalshi_fee, pm_fee, vwap
from .models import Book
from .venues.util import num

NY = ZoneInfo("America/New_York")
KS_DAILY_SERIES = {"btc": "KXBTCD", "eth": "KXETHD", "sol": "KXSOLD", "xrp": "KXXRPD"}
_MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
_EVT = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})$")
_NUM = r"([\d][\d,]*(?:\.\d+)?)"
_OR_ABOVE = re.compile(r"\$?\s*" + _NUM + r"\s*(?:or above|or more|and above)", re.I)
_AT_OR_ABOVE = re.compile(r"(?:at or above|at least|greater than or equal to)\s*\$?\s*" + _NUM, re.I)


# ---------- temps ----------
def pm_daily_slug(asset: str, d: date) -> str:
    return f"{asset}-above-day-{d:%Y-%m-%d}"


def pm_settle_from_date(d: date) -> datetime:
    """Règlement des marchés journaliers Polymarket : 17:00 heure de New York (EST/EDT géré par zoneinfo)."""
    return datetime(d.year, d.month, d.day, 17, 0, tzinfo=NY).astimezone(timezone.utc)


def event_of(ticker: str) -> str:
    """KXBTCD-26NOV0817-T73499.99 -> KXBTCD-26NOV0817."""
    return re.sub(r"-[TB][\d.]+$", "", ticker.upper())


def parse_ks_event_ticker(ev: str) -> datetime | None:
    """KXBTCD-26NOV0817 -> 2026-11-08 17:00 New York -> 22:00Z (21:00Z en heure d'été)."""
    m = _EVT.search(event_of(ev))
    if not m or m.group(2) not in _MONTHS:
        return None
    yy, mon, dd, hh = int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)), int(m.group(4))
    try:
        return datetime(2000 + yy, mon, dd, hh, 0, tzinfo=NY).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_iso(v) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# ---------- seuils ----------
def thr_key(x: float) -> Decimal:
    return round(Decimal(str(x)), 4)


def _to_float(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_threshold_text(text: str) -> float | None:
    """'$73,500 or above' -> 73500.0 ; 'at or above $1.15' -> 1.15."""
    for rx in (_OR_ABOVE, _AT_OR_ABOVE):
        m = rx.search(text or "")
        if m:
            return _to_float(m.group(1))
    return None


def ks_threshold(m: dict) -> float | None:
    """Seuil « or above » d'un marché Kalshi. Priorité au libellé ; sinon floor_strike (+1 tick si 'greater',
    car Kalshi écrit 'above 73499.99' pour '$73,500 or above')."""
    for k in ("yes_sub_title", "subtitle", "title"):
        t = parse_threshold_text(str(m.get(k) or ""))
        if t is not None:
            return t
    fs, st = m.get("floor_strike"), str(m.get("strike_type", "")).lower()
    if fs is not None:
        d = Decimal(str(fs))
        decimals = max(-d.as_tuple().exponent, 0)
        if st == "greater":
            return float(d + Decimal(1).scaleb(-decimals))
        if st in ("greater_or_equal", "at_least"):
            return float(d)
    return None


def pm_market_threshold(m: dict) -> float | None:
    for k in ("title", "question", "subtitle", "shortTitle", "groupItemTitle"):
        t = parse_threshold_text(str(m.get(k) or ""))
        if t is not None:
            return t
    t = parse_threshold_text(str(m.get("description") or ""))
    if t is not None:
        return t
    terms = m.get("assetPriceTerms") or {}
    for k in ("strike", "threshold", "target"):
        v = num(terms.get(k))
        if v is not None:
            return v
    return None


def parse_pm_daily_event(js: dict) -> dict | None:
    """Événement journalier Polymarket -> {slug, settle, markets:[{slug,threshold,...}], by_slug:{...}}."""
    from .venues.polymarket import parse_market  # import tardif : évite une dépendance circulaire
    ev = js.get("event", js) if isinstance(js, dict) else {}
    mk = ev.get("markets") if isinstance(ev, dict) else None
    if not isinstance(mk, list) or not mk:
        return None
    settle = parse_iso(ev.get("endDate")) or parse_iso((mk[0] or {}).get("endDate"))
    markets, by_slug = [], {}
    for m in mk:
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        info = parse_market(m)
        info["slug"], info["threshold"] = m["slug"], pm_market_threshold(m)
        by_slug[m["slug"]] = info
        if info["threshold"] is not None:
            markets.append(info)
    return {"slug": ev.get("slug"), "settle": settle, "markets": markets, "by_slug": by_slug}


def match_pairs(ks_markets: list[dict], pm_markets: list[dict]) -> list[tuple[float, dict, dict]]:
    """Seuils présents des deux côtés : [(seuil, marché_kalshi, marché_polymarket)] triés."""
    ks = {}
    for m in ks_markets:
        ks.setdefault(thr_key(m["threshold"]), m)
    pm = {}
    for m in pm_markets:
        pm.setdefault(thr_key(m["threshold"]), m)
    return [(float(k), ks[k], pm[k]) for k in sorted(set(ks) & set(pm))]


# ---------- cotes Kalshi (liste des marchés) ----------
def ks_px(m: dict, key: str) -> float | None:
    v = m.get(key + "_dollars")
    x = num(v) if v is not None else None
    if x is None:
        c = num(m.get(key))
        x = c / 100.0 if c is not None else None
    return x if x is not None and 0 < x < 1 else None


def ks_mid(m: dict) -> float | None:
    bid, ask = ks_px(m, "yes_bid"), ks_px(m, "yes_ask")
    if bid and ask:
        return (bid + ask) / 2
    return ask or bid


# ---------- évaluation ----------
def evaluate_pair(pm: Book, ks: Book, n: float, secs_to_settle: float, now_ts: float,
                  cfg: Config) -> Evaluation:
    """Même seuil des deux côtés : Yes d'un côté + No de l'autre paie exactement 1 si les deux résolvent pareil."""
    if (now_ts - pm.ts_recv > cfg.max_stale_s or now_ts - ks.ts_recv > cfg.max_stale_s
            or abs(pm.ts_recv - ks.ts_recv) > cfg.max_skew_s):
        return Evaluation("REJECT_STALE", "stale_or_skew")
    best: Evaluation | None = None
    for direction, (pm_side, ks_side) in DIRECTIONS.items():
        pm_lad = pm.asks_up if pm_side == "up" else pm.asks_down
        ks_lad = ks.asks_up if ks_side == "up" else ks.asks_down
        a, b = vwap(pm_lad, n), vwap(ks_lad, n)
        if a is None or b is None:
            continue
        fees = pm_fee(a, n, cfg.pm_fee_coef) + kalshi_fee(b, n, cfg.ks_fee_coef)
        cost_per = a + b + fees / n
        net = 1.0 - cost_per
        ev = Evaluation("OBSERVE", direction=direction, n=n, vwap_pm=a, vwap_ks=b, fees_total=fees,
                        cost_per=cost_per, gross=1.0 - (a + b), net=net, net_bps=net / cost_per * 1e4,
                        slippage=(a - pm.best(pm_side)) + (b - ks.best(ks_side)))
        if best is None or ev.net > best.net:
            best = ev
    if best is None:
        return Evaluation("REJECT_DEPTH", "depth<n")
    if best.net_bps >= cfg.daily_min_edge_bps:
        if secs_to_settle < cfg.daily_no_entry_last_s:
            best.reason = "last_minutes_regime"
        else:
            best.status = "ACTIONABLE"
    return best
