"""Table meta, protection de la DB et wipe.

- meta.engine_version : version du moteur qui a écrit les données
- meta.db_protected   : 'true' => la DB ne doit JAMAIS être wipée (ni au démarrage, ni via l'API)
- meta.data_since     : début de la période de données courante
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import db
from .version import ENGINE_VERSION

log = logging.getLogger("arb.maintenance")

WIPE_TABLES = ("quotes, signals, paper_trades, resolutions, windows, "
               "daily_signals, daily_trades, daily_resolutions, daily_pairs, daily_events, "
               "weather_signals, weather_trades, weather_resolutions, weather_pairs, signal_counts")   # audit_log, assets, meta gardés


def decide(stored_version: str | None, current_version: str, protected: bool) -> str:
    """'keep' (même version), 'keep_protected' (version changée mais DB protégée), 'wipe'."""
    if stored_version == current_version:
        return "keep"
    return "keep_protected" if protected else "wipe"


async def get_meta(pool, key: str) -> str | None:
    async with pool.acquire() as c:
        return await c.fetchval("SELECT value FROM meta WHERE key=$1", key)


async def all_meta(pool) -> dict[str, str]:
    async with pool.acquire() as c:
        return {r["key"]: r["value"] for r in await c.fetch("SELECT key, value FROM meta")}


async def set_meta(pool, key: str, value: str) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO meta(key,value) VALUES($1,$2)
               ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""", key, value)


async def is_protected(pool) -> bool:
    return (await get_meta(pool, "db_protected")) == "true"


async def set_protected(pool, flag: bool, actor: str = "api") -> None:
    await set_meta(pool, "db_protected", "true" if flag else "false")
    await db.audit(pool, actor, "protect" if flag else "unprotect", {})


async def wipe(pool, reason: str, actor: str = "system") -> None:
    if await is_protected(pool):
        raise PermissionError("DB protégée : wipe refusé")
    async with pool.acquire() as c:
        await c.execute(f"TRUNCATE {WIPE_TABLES} RESTART IDENTITY CASCADE")
    await set_meta(pool, "data_since", datetime.now(timezone.utc).isoformat())
    await db.audit(pool, actor, "wipe", {"reason": reason})
    log.warning("DB WIPED (%s)", reason)


async def startup(pool) -> str:
    stored = await get_meta(pool, "engine_version")
    protected = await is_protected(pool)
    action = decide(stored, ENGINE_VERSION, protected)
    if action == "wipe":
        await wipe(pool, f"engine {stored or 'none'} -> {ENGINE_VERSION}", actor="startup")
    elif action == "keep_protected":
        log.warning("Version moteur %s -> %s mais DB PROTEGEE : données conservées (versions mélangées)",
                    stored, ENGINE_VERSION)
        await db.audit(pool, "startup", "version_change_protected",
                       {"from": stored, "to": ENGINE_VERSION})
    if await get_meta(pool, "data_since") is None:
        await set_meta(pool, "data_since", datetime.now(timezone.utc).isoformat())
    if await get_meta(pool, "db_protected") is None:
        await set_meta(pool, "db_protected", "false")
    await set_meta(pool, "engine_version", ENGINE_VERSION)
    log.info("startup maintenance: action=%s version=%s protected=%s", action, ENGINE_VERSION, protected)
    return action
