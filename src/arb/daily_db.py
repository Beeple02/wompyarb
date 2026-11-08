"""Accès Postgres pour les marchés journaliers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .engine import Evaluation, dis_kind, payout


async def upsert_event(pool, asset, settle, ks_event, pm_event, n_ks, n_pm, n_overlap) -> int:
    async with pool.acquire() as c:
        return await c.fetchval(
            """INSERT INTO daily_events(asset,settle_ts,ks_event,pm_event,n_ks,n_pm,n_overlap)
               VALUES($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT (asset,settle_ts) DO UPDATE SET ks_event=EXCLUDED.ks_event,
                 pm_event=EXCLUDED.pm_event, n_ks=EXCLUDED.n_ks, n_pm=EXCLUDED.n_pm,
                 n_overlap=EXCLUDED.n_overlap, scanned_at=now() RETURNING id""",
            asset, settle, ks_event, pm_event, n_ks, n_pm, n_overlap)


async def upsert_pair(pool, event_id, asset, settle, threshold, ks_ticker, pm_slug) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO daily_pairs(event_id,asset,settle_ts,threshold,ks_ticker,pm_slug)
               VALUES($1,$2,$3,$4,$5,$6)
               ON CONFLICT (event_id,threshold) DO UPDATE SET ks_ticker=EXCLUDED.ks_ticker,
                 pm_slug=EXCLUDED.pm_slug""",
            event_id, asset, settle, threshold, ks_ticker, pm_slug)


async def deactivate_expired(pool) -> None:
    async with pool.acquire() as c:
        await c.execute("UPDATE daily_pairs SET active=FALSE WHERE active AND settle_ts < now()")


async def active_pairs(pool, asset: str):
    async with pool.acquire() as c:
        return await c.fetch(
            "SELECT * FROM daily_pairs WHERE asset=$1 AND active AND settle_ts > now() ORDER BY settle_ts, threshold",
            asset)


async def insert_signal(pool, pair_id: int, ev: Evaluation, secs: float) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO daily_signals(pair_id,ts,secs_to_settle,status,reason,direction,n,vwap_pm,vwap_ks,
                   fees_total,cost_per,gross,net,net_bps)
               VALUES($1,now(),$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
            pair_id, secs, ev.status, ev.reason, ev.direction, ev.n, ev.vwap_pm, ev.vwap_ks,
            ev.fees_total, ev.cost_per, ev.gross, ev.net, ev.net_bps)


async def has_trade(pool, pair_id: int) -> bool:
    async with pool.acquire() as c:
        return bool(await c.fetchval("SELECT 1 FROM daily_trades WHERE pair_id=$1", pair_id))


async def open_trade(pool, pair_id: int, ev: Evaluation) -> bool:
    async with pool.acquire() as c:
        r = await c.fetchval(
            """INSERT INTO daily_trades(pair_id,ts,direction,n,vwap_pm,vwap_ks,fees_total,expected_net)
               VALUES($1,now(),$2,$3,$4,$5,$6,$7) ON CONFLICT (pair_id) DO NOTHING RETURNING id""",
            pair_id, ev.direction, ev.n, ev.vwap_pm, ev.vwap_ks, ev.fees_total, ev.net * ev.n)
        return r is not None


async def pending_pairs(pool, delay_s: float, max_age_s: float = 72 * 3600):
    now = datetime.now(timezone.utc)
    async with pool.acquire() as c:
        return await c.fetch(
            """SELECT p.*, e.pm_event FROM daily_pairs p
               JOIN daily_events e ON e.id=p.event_id
               LEFT JOIN daily_resolutions r ON r.pair_id=p.id
               WHERE p.settle_ts < $1 AND p.settle_ts > $2 AND COALESCE(r.complete, FALSE) = FALSE
               ORDER BY p.settle_ts, p.threshold""",
            now - timedelta(seconds=delay_s), now - timedelta(seconds=max_age_s))


async def save_resolution(pool, pair_id: int, ks, pm, ks_settle=None, pm_settle=None) -> bool:
    complete = ks is not None and pm is not None
    kind = dis_kind(pm, ks) if complete else None
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                """INSERT INTO daily_resolutions(pair_id,ks_result,pm_result,ks_settle,pm_settle,dis_kind,complete)
                   VALUES($1,$2,$3,$4,$5,$6,$7)
                   ON CONFLICT (pair_id) DO UPDATE SET ks_result=EXCLUDED.ks_result,
                     pm_result=EXCLUDED.pm_result, dis_kind=EXCLUDED.dis_kind, complete=EXCLUDED.complete,
                     ks_settle=COALESCE(EXCLUDED.ks_settle, daily_resolutions.ks_settle),
                     pm_settle=COALESCE(EXCLUDED.pm_settle, daily_resolutions.pm_settle), updated_at=now()""",
                pair_id, ks, pm, ks_settle, pm_settle, kind, complete)
            if complete:
                for t in await c.fetch(
                        "SELECT id,direction,n,vwap_pm,vwap_ks,fees_total FROM daily_trades "
                        "WHERE pair_id=$1 AND status='open'", pair_id):
                    pay = payout(t["direction"], pm, ks) * t["n"]
                    pnl = pay - t["n"] * (t["vwap_pm"] + t["vwap_ks"]) - t["fees_total"]
                    await c.execute(
                        "UPDATE daily_trades SET status='settled',payout=$2,pnl=$3,settled_at=now() WHERE id=$1",
                        t["id"], pay, pnl)
                await c.execute("UPDATE daily_pairs SET active=FALSE WHERE id=$1", pair_id)
    return complete
