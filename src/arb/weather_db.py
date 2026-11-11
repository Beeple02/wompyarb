"""Accès Postgres pour les marchés météo (pluie par ville)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .engine import Evaluation, dis_kind, payout


async def upsert_pair(pool, p: dict) -> int:
    async with pool.acquire() as c:
        return await c.fetchval(
            """INSERT INTO weather_pairs(city,day,day_end,ks_ticker,pm_event,pm_slug,pm_token,ks_station,pm_station,
                   station_status,ks_trace,pm_trace,safe_direction,tradable,reason,ambiguous,ks_rules,pm_rules)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
               ON CONFLICT (city,day) DO UPDATE SET ks_ticker=EXCLUDED.ks_ticker, pm_event=EXCLUDED.pm_event,
                 pm_slug=EXCLUDED.pm_slug, pm_token=EXCLUDED.pm_token, ks_station=EXCLUDED.ks_station,
                 pm_station=EXCLUDED.pm_station, station_status=EXCLUDED.station_status,
                 ks_trace=EXCLUDED.ks_trace, pm_trace=EXCLUDED.pm_trace, safe_direction=EXCLUDED.safe_direction,
                 tradable=EXCLUDED.tradable, reason=EXCLUDED.reason, ambiguous=EXCLUDED.ambiguous,
                 ks_rules=EXCLUDED.ks_rules, pm_rules=EXCLUDED.pm_rules, last_seen=now()
               RETURNING id""",
            p["city"], p["day"], p["day_end"], p["ks_ticker"], p["pm_event"], p["pm_slug"], p["pm_token"],
            p["ks_station"], p["pm_station"], p["station_status"], p["ks_trace"], p["pm_trace"],
            p["safe_direction"], p["tradable"], p["reason"], p["ambiguous"],
            (p["ks_rules"] or "")[:1500], (p["pm_rules"] or "")[:1500])


async def deactivate_old(pool, grace_h: float = 6) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE weather_pairs SET active=FALSE WHERE active AND day_end < now() - make_interval(hours => $1::int)",
                        int(grace_h))


async def active_pairs(pool):
    async with pool.acquire() as c:
        return await c.fetch("SELECT * FROM weather_pairs WHERE active AND day_end > now() - interval '6 hours' "
                             "ORDER BY day, city")


async def insert_signal(pool, pair_id: int, ev: Evaluation, secs: float, status: str, reason: str) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO weather_signals(pair_id,ts,secs_to_end,status,reason,direction,n,vwap_pm,vwap_ks,
                   fees_total,cost_per,gross,net,net_bps)
               VALUES($1,now(),$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
            pair_id, secs, status, reason, ev.direction, ev.n, ev.vwap_pm, ev.vwap_ks, ev.fees_total,
            ev.cost_per, ev.gross, ev.net, ev.net_bps)


async def has_trade(pool, pair_id: int) -> bool:
    async with pool.acquire() as c:
        return bool(await c.fetchval("SELECT 1 FROM weather_trades WHERE pair_id=$1", pair_id))


async def open_trade(pool, pair_id: int, ev: Evaluation) -> bool:
    async with pool.acquire() as c:
        r = await c.fetchval(
            """INSERT INTO weather_trades(pair_id,ts,direction,n,vwap_pm,vwap_ks,fees_total,expected_net)
               VALUES($1,now(),$2,$3,$4,$5,$6,$7) ON CONFLICT (pair_id) DO NOTHING RETURNING id""",
            pair_id, ev.direction, ev.n, ev.vwap_pm, ev.vwap_ks, ev.fees_total, ev.net * ev.n)
        return r is not None


async def pending_pairs(pool, delay_s: float, max_age_s: float = 6 * 86400):
    now = datetime.now(timezone.utc)
    async with pool.acquire() as c:
        return await c.fetch(
            """SELECT p.* FROM weather_pairs p LEFT JOIN weather_resolutions r ON r.pair_id=p.id
               WHERE p.day_end < $1 AND p.day_end > $2 AND COALESCE(r.complete, FALSE) = FALSE
               ORDER BY p.day_end, p.city""",
            now - timedelta(seconds=delay_s), now - timedelta(seconds=max_age_s))


async def save_resolution(pool, pair_id: int, ks, pm) -> bool:
    complete = ks is not None and pm is not None
    kind = dis_kind(pm, ks) if complete else None
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                """INSERT INTO weather_resolutions(pair_id,ks_result,pm_result,dis_kind,complete)
                   VALUES($1,$2,$3,$4,$5)
                   ON CONFLICT (pair_id) DO UPDATE SET ks_result=EXCLUDED.ks_result, pm_result=EXCLUDED.pm_result,
                     dis_kind=EXCLUDED.dis_kind, complete=EXCLUDED.complete, updated_at=now()""",
                pair_id, ks, pm, kind, complete)
            if complete:
                for t in await c.fetch(
                        "SELECT id,direction,n,vwap_pm,vwap_ks,fees_total FROM weather_trades "
                        "WHERE pair_id=$1 AND status='open'", pair_id):
                    pay = payout(t["direction"], pm, ks) * t["n"]
                    pnl = pay - t["n"] * (t["vwap_pm"] + t["vwap_ks"]) - t["fees_total"]
                    await c.execute(
                        "UPDATE weather_trades SET status='settled',payout=$2,pnl=$3,settled_at=now() WHERE id=$1",
                        t["id"], pay, pnl)
                await c.execute("UPDATE weather_pairs SET active=FALSE WHERE id=$1", pair_id)
    return complete
