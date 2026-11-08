from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from . import api, daily, db, logbuf, maintenance, stats, windows
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


class State:
    """Etat partagé entre le collecteur et l'API."""

    def __init__(self, cfg: Config):
        self.started_at = time.time()
        self.known: dict = {}
        self.daily_stats: dict = {}   # asset -> dernier état du cycle journalier (exposé par /api/daily/overview)
        self.pm_404: dict = {}   # (asset, t_start) -> nb de 404 consécutifs sur le book Polymarket
        self.dis_cache = DisCache(cfg)

    def reset_caches(self) -> None:
        self.known.clear()
        self.pm_404.clear()
        self.daily_stats.clear()
        self.dis_cache.data, self.dis_cache.ts = {}, 0.0


def _is_404(e) -> bool:
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404


async def tick_asset(asset, cfg, pool, kal, pmc, known, dis_cache, state):
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
        for k in [k for k in state.pm_404 if k[1] < start]:
            del state.pm_404[k]
    wid, ks_ref, pm_ref = known[key]

    ks_b, pm_b = await asyncio.gather(kal.fetch_book(ks_ref), pmc.fetch_book(pm_ref),
                                      return_exceptions=True)
    for name, b in (("kalshi", ks_b), ("polymarket", pm_b)):
        if isinstance(b, Exception) and not (name == "polymarket" and _is_404(b)):
            log.warning("%s %s fetch_book: %r", asset, name, b)
    # Book Polymarket 404 = pas de book pour l'instant : on retente en silence, 1 log par fenêtre
    if _is_404(pm_b):
        n = state.pm_404.get(key, 0) + 1
        state.pm_404[key] = n
        if n == 1:
            try:
                body = pm_b.response.text[:200]
            except Exception:
                body = ""
            since = (datetime.now(timezone.utc) - start).total_seconds()
            log.info("%s polymarket book 404 à +%.0fs (%s) body=%r | %s", asset, since, pm_ref.ident,
                     body, await pmc.diagnose(pm_ref))
    elif key in state.pm_404 and not isinstance(pm_b, Exception):
        n = state.pm_404.pop(key)
        since = (datetime.now(timezone.utc) - start).total_seconds()
        log.info("%s polymarket book disponible à +%.0fs (après %d x 404)", asset, since, n)
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


async def collector_loop(cfg, pool, http, state):
    kal, pmc = KalshiClient(cfg, http), PolymarketClient(cfg, http)
    known, dis_cache = state.known, state.dis_cache
    while True:
        t0 = time.monotonic()
        try:
            await dis_cache.refresh(pool)
            res = await asyncio.gather(*[tick_asset(a, cfg, pool, kal, pmc, known, dis_cache, state)
                                         for a in cfg.assets], return_exceptions=True)
            for a, r in zip(cfg.assets, res):
                if isinstance(r, Exception):
                    log.error("%s tick error: %r", a, r)
        except Exception:
            log.exception("collector loop")
        await asyncio.sleep(max(0.0, cfg.poll_interval_s - (time.monotonic() - t0)))


def backoff_delay(attempt: int) -> float:
    """Délai avant la prochaine tentative de résolution : 20 s, 40 s, 80 s ... plafonné à 10 min."""
    return min(20.0 * (2 ** attempt), 600.0)


def implied_result(strike, settle) -> str | None:
    """Règle des deux venues : Up si valeur finale >= valeur de départ (égalité -> Up)."""
    if strike is None or settle is None:
        return None
    return "up" if settle >= strike else "down"


async def resolver_loop(cfg, pool, http):
    kal, pmc = KalshiClient(cfg, http), PolymarketClient(cfg, http)
    from .models import MarketRef
    attempts: dict[int, int] = {}
    next_try: dict[int, float] = {}
    while True:
        try:
            for w in await db.pending_windows(pool, cfg.resolve_delay_s):
                wid, now = w["id"], time.time()
                if next_try.get(wid, 0.0) > now:
                    continue
                n = attempts.get(wid, 0)
                ks_i = pm_i = None
                try:
                    ks_i = await kal.fetch_result(MarketRef("kalshi", w["ks_ticker"]))
                except Exception as e:
                    if n % 5 == 0:
                        log.warning("kalshi result %s (essai %d): %r", w["ks_ticker"], n + 1, e)
                try:
                    pm_i = await pmc.fetch_event_result(w["asset"], w["t_start"])
                except Exception as e:
                    if n % 5 == 0:
                        log.warning("pm result %s %s (essai %d): %r", w["asset"], w["t_start"], n + 1, e)
                ks = ks_i["result"] if ks_i else None
                pm = pm_i["result"] if pm_i else None
                ks_src = "official" if ks else None
                pm_src = "official" if pm else None
                # Cohérence interne : le résultat doit suivre la règle >= entre les deux valeurs
                for venue, info, res in (("kalshi", ks_i, ks), ("polymarket", pm_i, pm)):
                    imp = implied_result(info.get("strike"), info.get("settle")) if info else None
                    if res and imp and imp != res:
                        log.warning("INCOHERENT %s %s %s: result=%s mais settle/strike impliquent %s",
                                    venue, w["asset"], w["t_start"], res, imp)
                age = (datetime.now(timezone.utc) - w["t_end"]).total_seconds()
                if pm is None and age > 900:  # dernier recours : dernier prix PM avant clôture
                    mid = await db.last_pm_mid(pool, wid, w["t_end"])
                    if mid is not None and (mid >= 0.9 or mid <= 0.1):
                        pm, pm_src = ("up" if mid >= 0.9 else "down"), "inferred"
                done = await db.save_resolution(
                    pool, wid, ks, pm, ks_src, pm_src,
                    ks_strike=ks_i and ks_i.get("strike"), ks_settle=ks_i and ks_i.get("settle"),
                    pm_strike=pm_i and pm_i.get("strike"), pm_settle=pm_i and pm_i.get("settle"))
                if done:
                    attempts.pop(wid, None)
                    next_try.pop(wid, None)
                    log.info("RESOLVED %s %s ks=%s(%s) pm=%s(%s) strikes ks/pm=%s/%s",
                             w["asset"], w["t_start"], ks, ks_src, pm, pm_src,
                             ks_i and ks_i.get("strike"), pm_i and pm_i.get("strike"))
                else:
                    attempts[wid] = n + 1
                    next_try[wid] = now + backoff_delay(n)
        except Exception:
            log.exception("resolver loop")
        await asyncio.sleep(10)


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
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logbuf.install()
    if not cfg.database_url:
        raise SystemExit("DATABASE_URL manquant")
    pool = await db.connect(cfg.database_url)
    await db.init(pool)
    action = await maintenance.startup(pool)   # wipe auto si version changée et DB non protégée
    await db.audit(pool, "main", "start", {"assets": cfg.assets, "paper_size": cfg.paper_size,
                                           "maintenance": action})
    state = State(cfg)
    await api.start(cfg, pool, state)
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), limits=limits,
                                 headers={"User-Agent": "arbwatch-paper/0.1"}) as http:
        tasks = [collector_loop(cfg, pool, http, state), resolver_loop(cfg, pool, http),
                 prune_loop(cfg, pool)]
        if cfg.daily_enabled:
            tasks += [daily.daily_loop(cfg, pool, http, state), daily.daily_resolver_loop(cfg, pool, http)]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
if __name__ == "__main__":
    asyncio.run(main())
