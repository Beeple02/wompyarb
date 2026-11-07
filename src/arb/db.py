from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

from .engine import Evaluation, dis_kind, payout
from .models import Book

ASSET_SEED = {
    "btc": ("KXBTC15M", "CF Benchmarks BRTI (moyenne 60 s)"),
    "eth": ("KXETH15M", "CF Benchmarks ETH-RTI (moyenne 60 s) - NON VERIFIE"),
    "sol": ("KXSOL15M", "CF Benchmarks SOL-RTI (moyenne 60 s) - NON VERIFIE"),
    "xrp": ("KXXRP15M", "CF Benchmarks XRP-RTI (moyenne 60 s) - NON VERIFIE"),
}


async def connect(url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(url, min_size=1, max_size=5)


async def init(pool: asyncpg.Pool) -> None:
    sql = (Path(__file__).parent / "schema.sql").read_text()
    async with pool.acquire() as c:
        await c.execute(sql)
        for sym, (series, ks_idx) in ASSET_SEED.items():
            await c.execute(
                """INSERT INTO assets(symbol, ks_series, pm_prefix, ks_index, pm_index,
                       ks_tie_rule, pm_tie_rule, ks_void_rule, pm_void_rule,
                       g1_same_index, g2_same_tie_void, rules_verified_on, rules_verified_by, notes)
                   VALUES($1,$2,$3,$4,$5,'YES si egal','Up si egal (version intl)','?','?',
                          FALSE, NULL, DATE '2026-11-07', 'recherche web (BTC uniquement)',
                          'A verifier a la main sur polymarket.us')
                   ON CONFLICT (symbol) DO NOTHING""",
                sym, series, f"{sym}-updown-15m", ks_idx,
                "Chainlink Data Streams (point-in-time) - version intl, a verifier sur polymarket.us")


async def get_or_create_window(pool, asset: str, start: datetime, end: datetime,
                               ks_ticker: str, pm_slug: str) -> int:
    async with pool.acquire() as c:
        return await c.fetchval(
            """INSERT INTO windows(asset,t_start,t_end,ks_ticker,pm_slug) VALUES($1,$2,$3,$4,$5)
               ON CONFLICT (asset,t_start) DO UPDATE SET ks_ticker=EXCLUDED.ks_ticker,
                   pm_slug=EXCLUDED.pm_slug RETURNING id""",
            asset, start, end, ks_ticker, pm_slug)


async def insert_quote(pool, window_id: int, book: Book) -> None:
    ladders = {"up": book.asks_up[:5], "down": book.asks_down[:5]}
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO quotes(window_id,venue,ts_recv,best_ask_up,best_ask_down,depth_up,depth_down,ladders)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)""",
            window_id, book.venue, datetime.fromtimestamp(book.ts_recv, tz=timezone.utc),
            book.best("up"), book.best("down"), book.depth("up"), book.depth("down"),
            json.dumps(ladders))


async def insert_signal(pool, window_id: int, ev: Evaluation, secs_to_close: float) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO signals(window_id,ts,secs_to_close,status,reason,direction,n,vwap_pm,vwap_ks,
                   fees_total,cost_per,gross,net,net_bps,p_dis,slippage)
               VALUES($1,now(),$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
            window_id, secs_to_close, ev.status, ev.reason, ev.direction, ev.n, ev.vwap_pm, ev.vwap_ks,
            ev.fees_total, ev.cost_per, ev.gross, ev.net, ev.net_bps, ev.p_dis, ev.slippage)


async def open_paper_trade(pool, window_id: int, ev: Evaluation) -> bool:
    async with pool.acquire() as c:
        r = await c.fetchval(
            """INSERT INTO paper_trades(window_id,ts,direction,n,vwap_pm,vwap_ks,fees_total,expected_net)
               VALUES($1,now(),$2,$3,$4,$5,$6,$7) ON CONFLICT (window_id) DO NOTHING RETURNING id""",
            window_id, ev.direction, ev.n, ev.vwap_pm, ev.vwap_ks, ev.fees_total, ev.net * ev.n)
        return r is not None


async def has_trade(pool, window_id: int) -> bool:
    async with pool.acquire() as c:
        return bool(await c.fetchval("SELECT 1 FROM paper_trades WHERE window_id=$1", window_id))


async def pending_windows(pool, delay_s: float, max_age_s: float = 3 * 3600):
    now = datetime.now(timezone.utc)
    async with pool.acquire() as c:
        return await c.fetch(
            """SELECT w.* FROM windows w LEFT JOIN resolutions r ON r.window_id=w.id
               WHERE w.t_end < $1 AND w.t_end > $2 AND COALESCE(r.complete, FALSE) = FALSE
               ORDER BY w.t_end""",
            now - timedelta(seconds=delay_s), now - timedelta(seconds=max_age_s))


async def last_pm_mid(pool, window_id: int, t_end: datetime) -> float | None:
    """P(Up) approx. du dernier quote Polymarket, seulement s'il date de < 20 s avant la clôture."""
    async with pool.acquire() as c:
        r = await c.fetchrow(
            """SELECT best_ask_up, best_ask_down, ts_recv FROM quotes
               WHERE window_id=$1 AND venue='polymarket' AND ts_recv <= $2
               ORDER BY ts_recv DESC LIMIT 1""", window_id, t_end)
    if not r or r["best_ask_up"] is None or r["best_ask_down"] is None:
        return None
    if (t_end - r["ts_recv"]).total_seconds() > 20:
        return None
    return (r["best_ask_up"] + (1 - r["best_ask_down"])) / 2


async def save_resolution(pool, window_id: int, ks: str | None, pm: str | None,
                          ks_src: str | None, pm_src: str | None,
                          ks_strike=None, ks_settle=None, pm_strike=None, pm_settle=None) -> bool:
    complete = ks is not None and pm is not None
    kind = dis_kind(pm, ks) if complete else None
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                """INSERT INTO resolutions(window_id,ks_result,pm_result,ks_source,pm_source,dis_kind,complete,
                       ks_strike,ks_settle,pm_strike,pm_settle)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                   ON CONFLICT (window_id) DO UPDATE SET ks_result=EXCLUDED.ks_result,
                     pm_result=EXCLUDED.pm_result, ks_source=EXCLUDED.ks_source,
                     pm_source=EXCLUDED.pm_source, dis_kind=EXCLUDED.dis_kind,
                     complete=EXCLUDED.complete,
                     ks_strike=COALESCE(EXCLUDED.ks_strike, resolutions.ks_strike),
                     ks_settle=COALESCE(EXCLUDED.ks_settle, resolutions.ks_settle),
                     pm_strike=COALESCE(EXCLUDED.pm_strike, resolutions.pm_strike),
                     pm_settle=COALESCE(EXCLUDED.pm_settle, resolutions.pm_settle),
                     updated_at=now()""",
                window_id, ks, pm, ks_src, pm_src, kind, complete,
                ks_strike, ks_settle, pm_strike, pm_settle)
            if complete:
                for t in await c.fetch(
                        "SELECT id,direction,n,vwap_pm,vwap_ks,fees_total FROM paper_trades "
                        "WHERE window_id=$1 AND status='open'", window_id):
                    pay = payout(t["direction"], pm, ks) * t["n"]
                    pnl = pay - t["n"] * (t["vwap_pm"] + t["vwap_ks"]) - t["fees_total"]
                    await c.execute(
                        "UPDATE paper_trades SET status='settled',payout=$2,pnl=$3,settled_at=now() WHERE id=$1",
                        t["id"], pay, pnl)
    return complete


async def dis_counts(pool) -> dict[str, tuple[int, int, int]]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT w.asset, count(*) n,
                      count(*) FILTER (WHERE r.dis_kind='PU_KD') k1,
                      count(*) FILTER (WHERE r.dis_kind='PD_KU') k2
               FROM resolutions r JOIN windows w ON w.id=r.window_id
               WHERE r.complete AND r.ks_source='official' AND r.pm_source='official'
               GROUP BY w.asset""")
    return {r["asset"]: (r["n"], r["k1"], r["k2"]) for r in rows}


async def prune(pool, days: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with pool.acquire() as c:
        await c.execute("DELETE FROM quotes WHERE ts_recv < $1", cutoff)
        await c.execute("DELETE FROM signals WHERE ts < $1 AND status IN ('REJECT_STALE','REJECT_DEPTH')", cutoff)


async def audit(pool, actor: str, event: str, payload: dict) -> None:
    async with pool.acquire() as c:
        await c.execute("INSERT INTO audit_log(actor,event,payload) VALUES($1,$2,$3::jsonb)",
                        actor, event, json.dumps(payload))
