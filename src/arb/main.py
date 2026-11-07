from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from . import db, stats, windows
from .config import Config, load
from .engine import evaluate
from .venues.kalshi import KalshiClient
from .venues.polymarket import PolymarketClient

log = logging.getLogger("arb")


class DisCache:
    """p_dis / asymétrie empiriques par actif, rafraîchis périodiquement depuis la DB."""

    def __init__(self, cfg: Config):
        self.cfg, self.data, self.ts = cfg, {}, 0.0

    async def refresh(self, pool) -> None:
        if time.time() - self.ts < 60:
            return
        self.data, self.ts = await db.dis_counts(pool), time.time()

    def get(self, asset: str) -> stats.DisEstimate:
        n, k1, k2 = self.data.get(asset, (0, 0, 0))
        return stats.estimate(n, k1, k2, self.cfg.prior_p_dis, self.cfg.prior_strength)


async def tick_asset(asset, cfg, pool, kal, pmc, known, dis_cache):
    now = datetime.now(timezone.utc)
    start, end = windows.current(now)
    key = (asset, start)
    if key not in known:
        ks_ref, pm_ref = await asyncio.gather(kal.discover(asset, start, end),
                                              pmc.discover(asset, start, end))
        if not ks_ref or not pm_ref:
            log.warning("%s discovery incomplète (kalshi=%s pm=%s)", asset, ks_ref, pm_ref)
            return
        wid = await db.get_or_create_window(pool, asset, start, end, ks_ref.ident, pm_ref.ident)
        known[key] = (wid, ks_ref, pm_ref)
        for k in [k for k in known if k[1] < start]:
            del known[k]
    wid, ks_ref, pm_ref = known[key]

    ks_b, pm_b = await asyncio.gather(kal.fetch_book(ks_ref), pmc.fetch_book(pm_ref),
                                      return_exceptions=True)
    for name, b in (("kalshi", ks_b), ("polymarket", pm_b)):
        if isinstance(b, Exception):
            log.warning("%s %s fetch_book: %r", asset, name, b)
    if isinstance(ks_b, Exception) or isinstance(pm_b, Exception):
        return
    await db.insert_quote(pool, wid, ks_b)
    await db.insert_quote(pool, wid, pm_b)

    secs_to_close = (end - datetime.now(timezone.utc)).total_seconds()
    ev = evaluate(pm_b, ks_b, cfg.paper_size, secs_to_close, time.time(), cfg, dis_cache.get(asset))
    await db.insert_signal(pool, wid, ev, secs_to_close)
    if ev.status == "ACTIONABLE" and not await db.has_trade(pool, wid):
        if await db.open_paper_trade(pool, wid, ev):
            log.info("PAPER TRADE %s %s n=%s pm=%.3f ks=%.3f net/ct=%.4f (%.0f bps)",
                     asset, ev.direction, ev.n, ev.vwap_pm, ev.vwap_ks, ev.net, ev.net_bps)


async def collector_loop(cfg, pool, http):
    kal, pmc = KalshiClient(cfg, http), PolymarketClient(cfg, http)
    known: dict = {}
    dis_cache = DisCache(cfg)
    while True:
        t0 = time.monotonic()
        try:
            await dis_cache.refresh(pool)
            res = await asyncio.gather(*[tick_asset(a, cfg, pool, kal, pmc, known, dis_cache)
                                         for a in cfg.assets], return_exceptions=True)
            for a, r in zip(cfg.assets, res):
                if isinstance(r, Exception):
                    log.error("%s tick error: %r", a, r)
        except Exception:
            log.exception("collector loop")
        await asyncio.sleep(max(0.0, cfg.poll_interval_s - (time.monotonic() - t0)))


async def resolver_loop(cfg, pool, http):
    kal, pmc = KalshiClient(cfg, http), PolymarketClient(cfg, http)
    from .models import MarketRef
    while True:
        try:
            for w in await db.pending_windows(pool, cfg.resolve_delay_s):
                ks = pm = None
                try:
                    ks = await kal.fetch_result(MarketRef("kalshi", w["ks_ticker"]))
                except Exception as e:
                    log.warning("kalshi result %s: %r", w["ks_ticker"], e)
                try:
                    pm = await pmc.fetch_result(MarketRef("polymarket", w["pm_slug"]))
                except Exception as e:
                    log.warning("pm result %s: %r", w["pm_slug"], e)
                ks_src = "official" if ks else None
                pm_src = "official" if pm else None
                age = (datetime.now(timezone.utc) - w["t_end"]).total_seconds()
                if pm is None and age > 600:  # fallback : dernier prix PM avant clôture
                    mid = await db.last_pm_mid(pool, w["id"], w["t_end"])
                    if mid is not None and (mid >= 0.9 or mid <= 0.1):
                        pm, pm_src = ("up" if mid >= 0.9 else "down"), "inferred"
                done = await db.save_resolution(pool, w["id"], ks, pm, ks_src, pm_src)
                if done:
                    log.info("RESOLVED %s %s ks=%s(%s) pm=%s(%s)", w["asset"], w["t_start"],
                             ks, ks_src, pm, pm_src)
        except Exception:
            log.exception("resolver loop")
        await asyncio.sleep(20)


async def prune_loop(cfg, pool):
    while True:
        try:
            await db.prune(pool, cfg.retention_days)
        except Exception:
            log.exception("prune")
        await asyncio.sleep(6 * 3600)


async def main() -> None:
    cfg = load()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not cfg.database_url:
        raise SystemExit("DATABASE_URL manquant")
    pool = await db.connect(cfg.database_url)
    await db.init(pool)
    await db.audit(pool, "main", "start", {"assets": cfg.assets, "paper_size": cfg.paper_size})
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), limits=limits,
                                 headers={"User-Agent": "arbwatch-paper/0.1"}) as http:
        await asyncio.gather(collector_loop(cfg, pool, http), resolver_loop(cfg, pool, http),
                             prune_loop(cfg, pool))


if __name__ == "__main__":
    asyncio.run(main())
