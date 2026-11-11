"""Pluie par ville : Kalshi KXRAIN <-> Polymarket INTERNATIONAL.

Cycle : SCAN (villes/jours communs + règles vérifiées) -> QUOTES (books des deux côtés, paper trade dans la
seule direction qui paie toujours >= 1) -> RESOLVE (après la fin du jour climatologique) -> rescan en boucle.
Priorité au jour en cours : seuil d'écart plus bas aujourd'hui que pour demain (capital moins longtemps bloqué).
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from datetime import datetime, timezone

import httpx

from . import storage
from . import weather_db as wdb
from .config import Config
from .daily_core import evaluate_pair
from .models import MarketRef
from .weather_core import (classify_pair, day_end_utc, directions_for, local_std_today, parse_intl_rain_event,
                           parse_ks_rain_market)

log = logging.getLogger("arb.weather")


def backoff_delay(attempt: int) -> float:
    return min(300.0 * (2 ** attempt), 3600.0)


def trusted_set(cfg: Config) -> set[str]:
    return {c.strip().upper() for c in cfg.weather_trusted_cities.split(",") if c.strip()}


async def gather_weather(cfg: Config, kal, pmi, now: datetime) -> dict:
    """Interroge les deux venues et classe les couples (sans écriture en base ; réutilisé par probe_weather)."""
    ks_raw = await kal.list_markets("KXRAIN")
    ks: dict[tuple, dict] = {}
    for m in ks_raw:
        info = parse_ks_rain_market(m)
        if info:
            ks.setdefault((info["city"], info["day"]), info)
    events = await pmi.weather_events()
    pm: dict[tuple, dict] = {}
    for ev in events:
        for mk in parse_intl_rain_event(ev):
            pm.setdefault((mk["city"], mk["day"]), mk)
    if not pm:   # filet : la catégorie 'weather' n'expose pas toujours ces événements
        try:
            for ev in await pmi.search_events("where will it rain"):
                for mk in parse_intl_rain_event(ev):
                    pm.setdefault((mk["city"], mk["day"]), mk)
        except Exception as e:
            log.info("recherche Polymarket: %r", e)
    trusted = trusted_set(cfg)
    pairs, skipped_days = [], 0
    for key in sorted(set(ks) & set(pm), key=lambda k: (k[1], k[0])):
        city, day = key
        today = local_std_today(city, now)
        if (day - today).days > cfg.weather_days_ahead or day_end_utc(city, day) < now:
            skipped_days += 1
            continue
        cls = classify_pair(ks[key], pm[key], trusted)
        pairs.append({"city": city, "day": day, "day_end": day_end_utc(city, day), "ks_ticker": ks[key]["ticker"],
                      "pm_event": pm[key]["event_slug"], "pm_slug": pm[key]["slug"], "pm_token": pm[key]["token_yes"],
                      "ks_rules": ks[key]["rules"], "pm_rules": pm[key]["rules"], **cls})
    return {"ks_n": len(ks), "pm_n": len(pm), "pm_events": len(events), "pairs": pairs,
            "only_ks": sorted(f"{c}@{d}" for c, d in set(ks) - set(pm)),
            "only_pm": sorted(f"{c}@{d}" for c, d in set(pm) - set(ks)), "skipped_days": skipped_days}


async def scan(cfg, pool, kal, pmi, state) -> None:
    res = await gather_weather(cfg, kal, pmi, datetime.now(timezone.utc))
    for p in res["pairs"]:
        await wdb.upsert_pair(pool, p)
    await wdb.deactivate_old(pool)
    n_ok = sum(1 for p in res["pairs"] if p["tradable"])
    log.info("SCAN météo : kalshi=%d polymarket(intl)=%d communs=%d négociables=%d%s", res["ks_n"], res["pm_n"],
             len(res["pairs"]), n_ok, "" if res["pairs"] else " (voir scripts/probe_weather.py)")
    for p in res["pairs"]:
        if not p["tradable"]:
            log.info("SCAN météo : %s %s non négociable : %s", p["city"], p["day"], p["reason"])
    state.weather_stats.update({"last_scan": datetime.now(timezone.utc).isoformat(), "ks": res["ks_n"],
                                "pm": res["pm_n"], "common": len(res["pairs"]), "tradable": n_ok,
                                "only_ks": res["only_ks"][:30], "only_pm": res["only_pm"][:30]})


async def quote(cfg, pool, kal, pmi, state) -> None:
    pairs = await wdb.active_pairs(pool)
    if not pairs:
        return
    wcfg = dataclasses.replace(cfg, max_stale_s=cfg.weather_max_stale_s, max_skew_s=cfg.weather_max_stale_s)
    sem = asyncio.Semaphore(4)
    stats = {"pairs": len(pairs), "ok": 0, "no_book": 0, "errors": 0, "actionable": 0, "best_bps": None}

    async def one(p) -> None:
        async with sem:
            ks_b, pm_b = await asyncio.gather(kal.fetch_book(MarketRef("kalshi", p["ks_ticker"])),
                                              pmi.fetch_book(MarketRef("polymarket", p["pm_token"])),
                                              return_exceptions=True)
        for b in (ks_b, pm_b):
            if isinstance(b, httpx.HTTPStatusError) and b.response.status_code == 404:
                stats["no_book"] += 1
                return
            if isinstance(b, Exception):
                stats["errors"] += 1
                return
        stats["ok"] += 1
        now = datetime.now(timezone.utc)
        secs = (p["day_end"] - now).total_seconds()
        today = local_std_today(p["city"], now)
        thr = cfg.weather_min_edge_bps if p["day"] <= today else cfg.weather_min_edge_bps_next
        ev = evaluate_pair(pm_b, ks_b, cfg.weather_size, secs, time.time(), wcfg,
                           directions=directions_for(p["safe_direction"]), min_edge_bps=thr, no_entry_last_s=0)
        status, reason = ev.status, ev.reason
        if status == "ACTIONABLE" and not p["tradable"]:
            status, reason = "UNVERIFIED", p["reason"] or "non négociable"
        nb = ev.net_bps if ev.direction else None
        state.counters.add("weather", p["city"], status, nb)
        t = time.time()
        if storage.should_store(status, nb, state.last_store.get(("weather", p["id"])), t,
                                cfg.weather_observe_every_s, thr / 2, 600):
            await wdb.insert_signal(pool, p["id"], ev, secs, status, reason)
            state.last_store[("weather", p["id"])] = t
        if ev.direction and status in ("OBSERVE", "ACTIONABLE", "UNVERIFIED"):
            if stats["best_bps"] is None or ev.net_bps > stats["best_bps"]:
                stats["best_bps"] = ev.net_bps
        if status == "ACTIONABLE":
            stats["actionable"] += 1
            if not await wdb.has_trade(pool, p["id"]) and await wdb.open_trade(pool, p["id"], ev):
                log.info("WEATHER TRADE %s %s %s n=%s pm=%.3f ks=%.3f net/ct=%.4f (%.0f bps)", p["city"], p["day"],
                         ev.direction, ev.n, ev.vwap_pm, ev.vwap_ks, ev.net, ev.net_bps)

    res = await asyncio.gather(*[one(p) for p in pairs], return_exceptions=True)
    for r in res:
        if isinstance(r, Exception):
            stats["errors"] += 1
            log.warning("weather pair error: %r", r)
    stats["ts"] = datetime.now(timezone.utc).isoformat()
    state.weather_stats.update(stats)


async def weather_loop(cfg, pool, http, state) -> None:
    from .venues.kalshi import KalshiClient
    from .venues.polymarket_intl import PolymarketIntl
    kal, pmi = KalshiClient(cfg, http), PolymarketIntl(cfg, http)
    last_scan = 0.0
    while True:
        t0 = time.monotonic()
        try:
            if time.time() - last_scan >= cfg.weather_scan_interval_s:
                try:
                    await scan(cfg, pool, kal, pmi, state)
                except Exception as e:
                    log.warning("scan météo: %r", e)
                last_scan = time.time()
            await quote(cfg, pool, kal, pmi, state)
        except Exception:
            log.exception("weather loop")
        await asyncio.sleep(max(0.0, cfg.weather_poll_s - (time.monotonic() - t0)))


async def weather_resolver_loop(cfg, pool, http) -> None:
    from .venues.kalshi import KalshiClient
    from .venues.polymarket_intl import PolymarketIntl, parse_intl_result
    kal, pmi = KalshiClient(cfg, http), PolymarketIntl(cfg, http)
    attempts: dict[int, int] = {}
    next_try: dict[int, float] = {}
    while True:
        try:
            for p in await wdb.pending_pairs(pool, cfg.weather_resolve_delay_s):
                pid, now = p["id"], time.time()
                if next_try.get(pid, 0.0) > now:
                    continue
                n = attempts.get(pid, 0)
                ks = pm = None
                try:
                    ks = (await kal.fetch_result(MarketRef("kalshi", p["ks_ticker"])))["result"]
                except Exception as e:
                    log.warning("weather kalshi result %s (essai %d): %r", p["ks_ticker"], n + 1, e)
                try:
                    m = await pmi.fetch_market(p["pm_slug"])
                    pm = parse_intl_result(m)["result"] if m else None
                except Exception as e:
                    log.warning("weather pm result %s (essai %d): %r", p["pm_slug"], n + 1, e)
                if await wdb.save_resolution(pool, pid, ks, pm):
                    attempts.pop(pid, None)
                    next_try.pop(pid, None)
                    log.info("WEATHER RESOLVED %s %s ks=%s pm=%s", p["city"], p["day"], ks, pm)
                else:
                    attempts[pid] = n + 1
                    next_try[pid] = now + backoff_delay(n)
        except Exception:
            log.exception("weather resolver loop")
        await asyncio.sleep(300)
