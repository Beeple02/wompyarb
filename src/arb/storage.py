"""Maîtrise de la taille de la base (plafond Railway ~500 Mo) : échantillonnage, compteurs agrégés, rétention, garde-fou.

Principes :
  * les évaluations REJECT_* ne sont jamais stockées une à une : elles sont comptées (table signal_counts) ;
  * les OBSERVE sont échantillonnées (1 toutes les N secondes par actif/paire), sauf quasi-opportunités ;
  * ACTIONABLE / UNVERIFIED sont toujours stockées ; les trades, résolutions, paires et l'audit ne sont jamais purgés ;
  * les cotes brutes sont échantillonnées et gardées 24 h ; les signaux 3 jours ;
  * garde-fou : au-delà de 70 % du plafond on purge plus court, au-delà de 90 % on vide cotes et signaux (TRUNCATE
    libère le disque tout de suite, contrairement à DELETE).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from . import db

log = logging.getLogger("arb.storage")
HEAVY_SIGNAL_TABLES = ("signals", "daily_signals", "weather_signals")


def should_store(status: str, net_bps: float | None, last_ts: float | None, now: float,
                 every_s: float, edge_floor_bps: float, reject_every_s: float | None = None) -> bool:
    if status in ("ACTIONABLE", "UNVERIFIED"):
        return True
    if status.startswith("REJECT"):   # jamais, sauf trace espacée demandée (météo/journalier : voir les books vides)
        return reject_every_s is not None and (last_ts is None or now - last_ts >= reject_every_s)
    if net_bps is not None and net_bps >= edge_floor_bps:      # quasi-opportunité : on garde
        return True
    return last_ts is None or now - last_ts >= every_s


def plan(size_mb: float, max_mb: float, soft: float = 0.70, hard: float = 0.90) -> str:
    if size_mb >= max_mb * hard:
        return "truncate"
    if size_mb >= max_mb * soft:
        return "prune"
    return "ok"


class Counters:
    """Compteurs horaires en mémoire, vidés en base toutes les minutes (1 ligne par heure/scope/clé/statut)."""

    def __init__(self) -> None:
        self.data: dict[tuple, list] = {}

    def add(self, scope: str, key: str, status: str, net_bps: float | None, ts: float | None = None) -> None:
        hour = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
        e = self.data.setdefault((hour, scope, key, status), [0, None, 0.0])
        e[0] += 1
        v = float(net_bps) if net_bps is not None else 0.0
        e[1] = v if e[1] is None else max(e[1], v)
        e[2] += v

    def drain(self) -> dict:
        d, self.data = self.data, {}
        return d

    async def flush(self, pool) -> int:
        d = self.drain()
        if not d:
            return 0
        try:
            async with pool.acquire() as c:
                for (hour, scope, key, status), (n, best, total) in d.items():
                    await c.execute(
                        """INSERT INTO signal_counts(hour,scope,key,status,n,best_bps,sum_bps)
                           VALUES($1,$2,$3,$4,$5,$6,$7)
                           ON CONFLICT (hour,scope,key,status) DO UPDATE SET n=signal_counts.n+EXCLUDED.n,
                             best_bps=GREATEST(signal_counts.best_bps, EXCLUDED.best_bps),
                             sum_bps=signal_counts.sum_bps+EXCLUDED.sum_bps""",
                        hour, scope, key, status, n, best, total)
        except Exception:
            for k, (n, best, total) in d.items():     # on remet en file : rien n'est perdu
                e = self.data.setdefault(k, [0, None, 0.0])
                e[0] += n
                e[1] = best if e[1] is None else max(e[1], best)
                e[2] += total
            raise
        return len(d)


async def db_size_mb(pool) -> float:
    async with pool.acquire() as c:
        return float(await c.fetchval("SELECT pg_database_size(current_database())")) / 1e6


async def table_sizes(pool, top: int = 8) -> list[dict]:
    async with pool.acquire() as c:
        rs = await c.fetch(
            """SELECT c.relname AS name, pg_total_relation_size(c.oid) AS bytes
               FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relkind='r' ORDER BY 2 DESC LIMIT $1""", top)
    return [{"name": r["name"], "mb": round(r["bytes"] / 1e6, 2)} for r in rs]


async def maintain(pool, cfg) -> str:
    """Rétention normale puis garde-fou de taille. Renvoie l'action : ok | prune | truncate."""
    now = datetime.now(timezone.utc)
    q_cut = now - timedelta(hours=cfg.quotes_retention_hours)
    s_cut = now - timedelta(days=cfg.signals_retention_days)
    async with pool.acquire() as c:
        await c.execute("DELETE FROM quotes WHERE ts_recv < $1", q_cut)
        for t in HEAVY_SIGNAL_TABLES:
            await c.execute(f"DELETE FROM {t} WHERE ts < $1", s_cut)
        await c.execute("DELETE FROM signal_counts WHERE hour < $1", now - timedelta(days=30))
    size = await db_size_mb(pool)
    action = plan(size, cfg.db_max_mb)
    if action == "prune":
        async with pool.acquire() as c:
            await c.execute("DELETE FROM quotes WHERE ts_recv < $1", now - timedelta(hours=cfg.quotes_retention_hours / 2))
            for t in HEAVY_SIGNAL_TABLES:
                await c.execute(f"DELETE FROM {t} WHERE ts < $1", now - timedelta(days=cfg.signals_retention_days / 2))
        log.warning("STORAGE %.0f Mo / %.0f Mo : purge anticipée des cotes et signaux", size, cfg.db_max_mb)
    elif action == "truncate":
        async with pool.acquire() as c:
            await c.execute("TRUNCATE quotes, signals, daily_signals, weather_signals")
        log.error("STORAGE %.0f Mo / %.0f Mo : TRUNCATE des cotes et signaux (trades et résolutions conservés)",
                  size, cfg.db_max_mb)
    if action != "ok":
        await db.audit(pool, "storage", action, {"size_mb": round(size, 1), "max_mb": cfg.db_max_mb})
    return action


async def maintain_loop(cfg, pool, state) -> None:
    last_guard = 0.0
    while True:
        try:
            await state.counters.flush(pool)
            if time.time() - last_guard >= 300:
                await maintain(pool, cfg)
                last_guard = time.time()
        except Exception:
            log.exception("maintain")
        await asyncio.sleep(60)
