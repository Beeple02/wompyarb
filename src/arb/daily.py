"""Marchés journaliers above/below : scanner de seuils communs, trading paper jusqu'au règlement, rescan.

Cycle de vie (en boucle) :
  1. SCAN (toutes les `daily_scan_interval_s`) : liste les marchés Kalshi ouverts, charge les événements
     Polymarket des prochains jours, ne garde que les couples (même heure de règlement, même seuil).
  2. QUOTES (toutes les `daily_poll_s`) : pour les seuils proches de la monnaie, lit les deux books,
     évalue, ouvre au plus 1 paper trade par seuil et par direction ACTIONABLE.
  3. RESOLVE : après le règlement (17:00 ET), lit les résultats officiels et règle les trades.
Quand l'horloge repasse sur le jour suivant, le scan suivant trouve les nouveaux événements : la boucle continue.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import daily_db as dd
from .config import Config
from .daily_core import (KS_DAILY_SERIES, NY, evaluate_pair, event_of, ks_mid, ks_threshold, match_pairs,
                         parse_ks_event_ticker, parse_pm_daily_event, pm_daily_slug, pm_settle_from_date,
                         thr_key)
from .models import MarketRef

log = logging.getLogger("arb.daily")


def backoff_delay(attempt: int) -> float:
    return min(60.0 * (2 ** attempt), 1800.0)


async def gather_asset(cfg: Config, kal, pmc, asset: str, now: datetime) -> dict:
    """Interroge les deux venues et calcule les seuils communs, sans écrire en base (réutilisé par probe_daily)."""
    ks_markets = await kal.list_markets(KS_DAILY_SERIES[asset])
    ks_events: dict[str, dict] = {}
    skipped = 0
    for m in ks_markets:
        ev = m.get("event_ticker") or event_of(m.get("ticker", ""))
        settle, th = parse_ks_event_ticker(ev), ks_threshold(m)
        if settle is None or th is None or not m.get("ticker"):
            skipped += 1
            continue
        d = ks_events.setdefault(ev, {"settle": settle, "markets": {}})
        d["markets"].setdefault(thr_key(th), {"ticker": m["ticker"], "threshold": th})
    out = {"asset": asset, "ks_events": {k: {"settle": v["settle"], "n": len(v["markets"])}
                                          for k, v in ks_events.items()},
           "ks_skipped": skipped, "matches": [], "notes": []}
    today_et = now.astimezone(NY).date()
    for i in range(cfg.daily_days_ahead + 1):
        d = today_et + timedelta(days=i)
        slug = pm_daily_slug(asset, d)
        try:
            pmev = parse_pm_daily_event(await pmc.fetch_event(slug))
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                out["notes"].append(f"{slug}: HTTP {e.response.status_code}")
            continue
        if not pmev or not pmev["markets"]:
            out["notes"].append(f"{slug}: aucun marché avec seuil lisible")
            continue
        pm_settle = pmev["settle"] or pm_settle_from_date(d)
        if pm_settle <= now:
            continue
        hit = next(((ev, kd) for ev, kd in ks_events.items()
                    if abs((kd["settle"] - pm_settle).total_seconds()) <= cfg.daily_settle_tol_s), None)
        if hit is None:
            out["notes"].append(f"{slug}: aucun événement Kalshi à ±{cfg.daily_settle_tol_s:.0f}s de {pm_settle:%Y-%m-%d %H:%MZ}")
            continue
        ks_ev, kd = hit
        pairs = match_pairs(list(kd["markets"].values()), pmev["markets"])
        out["matches"].append({"settle": kd["settle"], "ks_event": ks_ev, "pm_event": slug,
                               "n_ks": len(kd["markets"]), "n_pm": len(pmev["markets"]),
                               "pairs": [(th, k["ticker"], p["slug"]) for th, k, p in pairs]})
    return out


async def scan_asset(cfg, pool, kal, pmc, asset: str, state) -> None:
    res = await gather_asset(cfg, kal, pmc, asset, datetime.now(timezone.utc))
    for note in res["notes"]:
        log.info("SCAN %s : %s", asset, note)
    for m in res["matches"]:
        event_id = await dd.upsert_event(pool, asset, m["settle"], m["ks_event"], m["pm_event"],
                                         m["n_ks"], m["n_pm"], len(m["pairs"]))
        for th, ticker, slug in m["pairs"]:
            await dd.upsert_pair(pool, event_id, asset, m["settle"], th, ticker, slug)
        log.info("SCAN %s règlement %s : kalshi=%d polymarket=%d communs=%d", asset,
                 m["settle"].strftime("%Y-%m-%d %H:%MZ"), m["n_ks"], m["n_pm"], len(m["pairs"]))
    state.daily_stats.setdefault(asset, {})["last_scan"] = datetime.now(timezone.utc).isoformat()


async def quote_asset(cfg, pool, kal, pmc, asset: str, state) -> None:
    pairs = await dd.active_pairs(pool, asset)
    if not pairs:
        return
    ks_markets = await kal.list_markets(KS_DAILY_SERIES[asset])
    mids = {m.get("ticker"): ks_mid(m) for m in ks_markets}
    cand = [p for p in pairs
            if (mids.get(p["ks_ticker"]) is not None
                and cfg.daily_band_lo <= mids[p["ks_ticker"]] <= cfg.daily_band_hi)]
    sem = asyncio.Semaphore(cfg.daily_concurrency)
    stats = {"pairs": len(pairs), "candidates": len(cand), "ok": 0, "no_book": 0, "errors": 0,
             "actionable": 0, "best_bps": None}

    async def one(p) -> None:
        async with sem:
            ks_b, pm_b = await asyncio.gather(kal.fetch_book(MarketRef("kalshi", p["ks_ticker"])),
                                              pmc.fetch_book(MarketRef("polymarket", p["pm_slug"])),
                                              return_exceptions=True)
        for b in (ks_b, pm_b):
            if isinstance(b, httpx.HTTPStatusError) and b.response.status_code == 404:
                stats["no_book"] += 1
                return
            if isinstance(b, Exception):
                stats["errors"] += 1
                return
        stats["ok"] += 1
        secs = (p["settle_ts"] - datetime.now(timezone.utc)).total_seconds()
        ev = evaluate_pair(pm_b, ks_b, cfg.daily_size, secs, time.time(), cfg)
        await dd.insert_signal(pool, p["id"], ev, secs)
        if ev.net_bps is not None and ev.status in ("OBSERVE", "ACTIONABLE"):
            if stats["best_bps"] is None or ev.net_bps > stats["best_bps"]:
                stats["best_bps"] = ev.net_bps
        if ev.status == "ACTIONABLE":
            stats["actionable"] += 1
            if not await dd.has_trade(pool, p["id"]) and await dd.open_trade(pool, p["id"], ev):
                log.info("DAILY TRADE %s %s seuil=%s n=%s pm=%.3f ks=%.3f net/ct=%.4f (%.0f bps) règlement=%s",
                         asset, ev.direction, p["threshold"], ev.n, ev.vwap_pm, ev.vwap_ks, ev.net, ev.net_bps,
                         p["settle_ts"].strftime("%m-%d %H:%MZ"))

    res = await asyncio.gather(*[one(p) for p in cand], return_exceptions=True)
    for r in res:
        if isinstance(r, Exception):
            stats["errors"] += 1
            log.warning("daily %s pair error: %r", asset, r)
    stats["ts"] = datetime.now(timezone.utc).isoformat()
    state.daily_stats.setdefault(asset, {}).update(stats)


async def daily_loop(cfg, pool, http, state) -> None:
    from .venues.kalshi import KalshiClient
    from .venues.polymarket import PolymarketClient
    kal, pmc = KalshiClient(cfg, http), PolymarketClient(cfg, http)
    last_scan = 0.0
    while True:
        t0 = time.monotonic()
        try:
            if time.time() - last_scan >= cfg.daily_scan_interval_s:
                await dd.deactivate_expired(pool)
                for asset in cfg.assets:
                    try:
                        await scan_asset(cfg, pool, kal, pmc, asset, state)
                    except Exception as e:
                        log.warning("scan %s: %r", asset, e)
                last_scan = time.time()
            for asset in cfg.assets:
                try:
                    await quote_asset(cfg, pool, kal, pmc, asset, state)
                except Exception as e:
                    log.warning("quotes %s: %r", asset, e)
        except Exception:
            log.exception("daily loop")
        await asyncio.sleep(max(0.0, cfg.daily_poll_s - (time.monotonic() - t0)))


async def daily_resolver_loop(cfg, pool, http) -> None:
    from .venues.kalshi import KalshiClient
    from .venues.polymarket import PolymarketClient
    kal, pmc = KalshiClient(cfg, http), PolymarketClient(cfg, http)
    attempts: dict[int, int] = {}
    next_try: dict[int, float] = {}
    while True:
        try:
            ev_cache: dict[str, dict | None] = {}
            for p in await dd.pending_pairs(pool, cfg.daily_resolve_delay_s):
                pid, now = p["id"], time.time()
                if next_try.get(pid, 0.0) > now:
                    continue
                n = attempts.get(pid, 0)
                ks_i = pm_i = None
                try:
                    ks_i = await kal.fetch_result(MarketRef("kalshi", p["ks_ticker"]))
                except Exception as e:
                    if n % 5 == 0:
                        log.warning("daily kalshi result %s (essai %d): %r", p["ks_ticker"], n + 1, e)
                try:
                    key = p["pm_event"]
                    if key not in ev_cache:
                        ev_cache[key] = parse_pm_daily_event(await pmc.fetch_event(key))
                    pe = ev_cache[key]
                    pm_i = pe["by_slug"].get(p["pm_slug"]) if pe else None
                except Exception as e:
                    if n % 5 == 0:
                        log.warning("daily pm result %s (essai %d): %r", p["pm_event"], n + 1, e)
                ks = ks_i["result"] if ks_i else None
                pm = pm_i["result"] if pm_i else None
                done = await dd.save_resolution(pool, pid, ks, pm, ks_i and ks_i.get("settle"),
                                                pm_i and pm_i.get("settle"))
                if done:
                    attempts.pop(pid, None)
                    next_try.pop(pid, None)
                    log.info("DAILY RESOLVED %s seuil=%s ks=%s pm=%s", p["asset"], p["threshold"], ks, pm)
                else:
                    attempts[pid] = n + 1
                    next_try[pid] = now + backoff_delay(n)
        except Exception:
            log.exception("daily resolver loop")
        await asyncio.sleep(60)
