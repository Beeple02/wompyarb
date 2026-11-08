"""API HTTP du moteur (aiohttp), consommée par le dashboard local.

Auth : en-têtes X-API-Id et X-API-Key (valeurs = variables d'env API_ID / API_KEY).
Sans API_ID / API_KEY (>= 16 caractères pour la clé), l'API ne démarre pas.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hmac
import json
import logging
import time
from datetime import date, datetime, timezone
from decimal import Decimal

from aiohttp import web

from . import db, logbuf, maintenance
from .stats import wilson
from .version import ENGINE_VERSION

log = logging.getLogger("arb.api")
PUBLIC_PATHS = {"/api/health"}


# ---------- helpers ----------
def check_auth(api_id: str, api_key: str, headers) -> bool:
    if not api_id or not api_key:
        return False
    i, k = headers.get("X-API-Id", ""), headers.get("X-API-Key", "")
    ok_i = hmac.compare_digest(i.encode(), api_id.encode())
    ok_k = hmac.compare_digest(k.encode(), api_key.encode())
    return ok_i and ok_k


def _default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


def jr(data, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(data, default=_default), status=status,
                        content_type="application/json")


def rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


def qint(req: web.Request, name: str, default: int, lo: int = 1, hi: int = 10000) -> int:
    try:
        v = int(req.query.get(name, default))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def qstr(req: web.Request, name: str) -> str | None:
    v = req.query.get(name)
    return v.lower() if v else None


async def body(req: web.Request) -> dict:
    try:
        b = await req.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


# ---------- middlewares ----------
@web.middleware
async def cors_mw(req: web.Request, handler):
    origins = req.app["cfg"].api_cors
    hdrs = {"Access-Control-Allow-Origin": origins,
            "Access-Control-Allow-Headers": "X-API-Id, X-API-Key, Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Max-Age": "600"}
    if req.method == "OPTIONS":
        return web.Response(status=204, headers=hdrs)
    resp = await handler(req)
    resp.headers.update(hdrs)
    return resp


@web.middleware
async def error_mw(req: web.Request, handler):
    try:
        return await handler(req)
    except web.HTTPException as e:
        return jr({"error": e.reason}, e.status)
    except Exception as e:
        log.exception("api error %s", req.path)
        return jr({"error": repr(e)}, 500)


@web.middleware
async def auth_mw(req: web.Request, handler):
    if req.path in PUBLIC_PATHS:
        return await handler(req)
    cfg = req.app["cfg"]
    if not check_auth(cfg.api_id, cfg.api_key, req.headers):
        await asyncio.sleep(0.5)  # freine le brute force
        return jr({"error": "unauthorized"}, 401)
    return await handler(req)


# ---------- handlers ----------
async def health(req):
    return jr({"ok": True, "engine_version": ENGINE_VERSION,
               "uptime_s": round(time.time() - req.app["state"].started_at)})


async def status(req):
    pool, st, cfg = req.app["pool"], req.app["state"], req.app["cfg"]
    meta = await maintenance.all_meta(pool)
    async with pool.acquire() as c:
        counts = {}
        for t in ("windows", "paper_trades", "resolutions", "audit_log"):
            counts[t] = await c.fetchval(f"SELECT count(*) FROM {t}")
        for t in ("quotes", "signals"):  # grosses tables : estimation rapide
            est = await c.fetchval("SELECT reltuples::bigint FROM pg_class WHERE relname=$1", t)
            counts[t] = est if est and est > 0 else await c.fetchval(f"SELECT count(*) FROM {t}")
        last_q = rows(await c.fetch(
            """SELECT w.asset, q.venue, max(q.ts_recv) AS last_ts FROM quotes q
               JOIN windows w ON w.id=q.window_id
               WHERE q.ts_recv > now() - interval '10 minutes' GROUP BY 1,2 ORDER BY 1,2"""))
        last_res = await c.fetchval("SELECT max(updated_at) FROM resolutions WHERE complete")
        open_trades = await c.fetchval("SELECT count(*) FROM paper_trades WHERE status='open'")
    return jr({"engine_version": ENGINE_VERSION, "uptime_s": round(time.time() - st.started_at),
               "db_protected": meta.get("db_protected") == "true", "data_since": meta.get("data_since"),
               "counts_approx": counts, "open_trades": open_trades, "last_quotes": last_q,
               "last_resolution": last_res, "assets": cfg.assets, "now": datetime.now(timezone.utc)})


async def config(req):
    d = dataclasses.asdict(req.app["cfg"])
    for k in ("database_url", "api_id", "api_key"):
        d.pop(k, None)
    return jr(d)


async def live(req):
    """Dernier signal et dernières cotes par actif : panneau temps réel."""
    async with req.app["pool"].acquire() as c:
        sig = rows(await c.fetch(
            """SELECT DISTINCT ON (w.asset) w.asset, s.ts, s.status, s.reason, s.direction, s.n,
                      s.vwap_pm, s.vwap_ks, s.gross, s.net, s.net_bps, s.p_dis, s.secs_to_close, w.t_end
               FROM signals s JOIN windows w ON w.id=s.window_id
               WHERE s.ts > now() - interval '2 minutes' ORDER BY w.asset, s.ts DESC"""))
        quo = rows(await c.fetch(
            """SELECT DISTINCT ON (w.asset, q.venue) w.asset, q.venue, q.ts_recv, q.best_ask_up,
                      q.best_ask_down, q.depth_up, q.depth_down
               FROM quotes q JOIN windows w ON w.id=q.window_id
               WHERE q.ts_recv > now() - interval '2 minutes' ORDER BY w.asset, q.venue, q.ts_recv DESC"""))
    return jr({"signals": sig, "quotes": quo})


async def windows_h(req):
    async with req.app["pool"].acquire() as c:
        rs = await c.fetch(
            """SELECT w.id, w.asset, w.t_start, w.t_end, w.ks_ticker, w.pm_slug, r.ks_result, r.pm_result,
                      r.ks_source, r.pm_source, r.dis_kind, COALESCE(r.complete,false) AS complete,
                      (SELECT count(*) FROM paper_trades p WHERE p.window_id=w.id) AS trades
               FROM windows w LEFT JOIN resolutions r ON r.window_id=w.id
               WHERE ($1::text IS NULL OR w.asset=$1) ORDER BY w.t_start DESC LIMIT $2""",
            qstr(req, "asset"), qint(req, "limit", 100, hi=2000))
    return jr(rows(rs))


async def quotes_h(req):
    withl = req.query.get("ladders") == "1"
    async with req.app["pool"].acquire() as c:
        rs = await c.fetch(
            f"""SELECT w.asset, q.venue, q.ts_recv, q.best_ask_up, q.best_ask_down, q.depth_up, q.depth_down
                {', q.ladders' if withl else ''}
                FROM quotes q JOIN windows w ON w.id=q.window_id
                WHERE q.ts_recv > now() - make_interval(mins => $3::int)
                  AND ($1::text IS NULL OR w.asset=$1) AND ($2::text IS NULL OR q.venue=$2)
                ORDER BY q.ts_recv DESC LIMIT $4""",
            qstr(req, "asset"), qstr(req, "venue"), qint(req, "minutes", 15, hi=1440),
            qint(req, "limit", 2000, hi=20000))
    out = rows(rs)
    if withl:
        for r in out:
            r["ladders"] = json.loads(r["ladders"]) if r["ladders"] else None
    return jr(out)


async def signals_h(req):
    status_f = req.query.get("status")
    async with req.app["pool"].acquire() as c:
        rs = await c.fetch(
            """SELECT w.asset, s.* FROM signals s JOIN windows w ON w.id=s.window_id
               WHERE s.ts > now() - make_interval(mins => $3::int)
                 AND ($1::text IS NULL OR w.asset=$1) AND ($2::text IS NULL OR s.status=$2)
               ORDER BY s.ts DESC LIMIT $4""",
            qstr(req, "asset"), status_f.upper() if status_f else None,
            qint(req, "minutes", 60, hi=10080), qint(req, "limit", 500, hi=10000))
    return jr(rows(rs))


async def trades_h(req):
    async with req.app["pool"].acquire() as c:
        rs = await c.fetch(
            """SELECT p.*, w.asset, w.t_start, w.t_end, (1 - p.vwap_pm - p.vwap_ks) AS gap,
                      CASE WHEN p.n > 0 THEN p.pnl / p.n END AS pnl_per_contract,
                      r.pm_result, r.ks_result, r.dis_kind
               FROM paper_trades p JOIN windows w ON w.id=p.window_id
               LEFT JOIN resolutions r ON r.window_id=p.window_id
               WHERE ($1::text IS NULL OR w.asset=$1) AND ($2::text IS NULL OR p.status=$2)
               ORDER BY p.ts DESC LIMIT $3""",
            qstr(req, "asset"), qstr(req, "status"), qint(req, "limit", 200, hi=5000))
    return jr(rows(rs))


async def resolutions_h(req):
    async with req.app["pool"].acquire() as c:
        rs = await c.fetch(
            """SELECT r.*, w.asset, w.t_start, w.t_end FROM resolutions r
               JOIN windows w ON w.id=r.window_id WHERE ($1::text IS NULL OR w.asset=$1)
               ORDER BY w.t_start DESC LIMIT $2""", qstr(req, "asset"), qint(req, "limit", 200, hi=5000))
    return jr(rows(rs))


async def stats_dis(req):
    pool, st = req.app["pool"], req.app["state"]
    counts = await db.dis_counts(pool)
    out = []
    for asset in req.app["cfg"].assets:
        n, k1, k2 = counts.get(asset, (0, 0, 0))
        lo, hi = wilson(k1 + k2, n)
        est = st.dis_cache.get(asset)
        out.append({"asset": asset, "n": n, "disagreements": k1 + k2, "p_hat": (k1 + k2) / n if n else None,
                    "ci95": [lo, hi], "PU_KD": k1, "PD_KU": k2,
                    "p_dis_used_by_engine": est.p_dis, "asym_used_by_engine": est.asym})
    async with pool.acquire() as c:
        inferred = await c.fetchval(
            "SELECT count(*) FROM resolutions WHERE complete AND (ks_source<>'official' OR pm_source<>'official')")
    return jr({"by_asset": out, "excluded_inferred": inferred})


async def stats_pnl(req):
    async with req.app["pool"].acquire() as c:
        by_asset = rows(await c.fetch(
            """SELECT w.asset, count(*) n, sum(pnl) pnl, avg(pnl) mean, stddev_samp(pnl) sd,
                      avg((pnl>0)::int) win, sum(p.n*(vwap_pm+vwap_ks)+fees_total) capital
               FROM paper_trades p JOIN windows w ON w.id=p.window_id
               WHERE p.status='settled' GROUP BY 1 ORDER BY 1"""))
        by_gap = rows(await c.fetch(
            """SELECT floor((1 - vwap_pm - vwap_ks)/0.02)*0.02 AS gap_from, count(*) n,
                      avg(pnl / n) AS pnl_per_contract, avg((pnl>0)::int) win
               FROM paper_trades WHERE status='settled' GROUP BY 1 ORDER BY 1"""))
        payouts = rows(await c.fetch(
            """SELECT (payout / n) AS payout_per_contract, count(*) n FROM paper_trades
               WHERE status='settled' GROUP BY 1 ORDER BY 1"""))
        curve = rows(await c.fetch(
            """SELECT settled_at, pnl, sum(pnl) OVER (ORDER BY settled_at, id) AS cumulative
               FROM paper_trades WHERE status='settled' ORDER BY settled_at, id LIMIT 5000"""))
    return jr({"by_asset": by_asset, "by_gap": by_gap, "payouts": payouts, "equity_curve": curve})


async def stats_signals(req):
    async with req.app["pool"].acquire() as c:
        rs = await c.fetch(
            """SELECT w.asset, s.status, count(*) n, max(s.net_bps) best_bps, avg(s.net_bps) avg_bps
               FROM signals s JOIN windows w ON w.id=s.window_id
               WHERE s.ts > now() - make_interval(hours => $1::int) GROUP BY 1,2 ORDER BY 1,2""",
            qint(req, "hours", 24, hi=168))
    return jr(rows(rs))


async def stats_basis(req):
    """Compare les valeurs de référence (strike) et de règlement des deux venues, fenêtre par fenêtre."""
    async with req.app["pool"].acquire() as c:
        agg = rows(await c.fetch(
            """SELECT w.asset, count(*) n,
                      count(*) FILTER (WHERE r.pm_strike = r.ks_strike) same_strike,
                      count(*) FILTER (WHERE r.pm_settle = r.ks_settle) same_settle,
                      avg(abs(r.pm_strike - r.ks_strike) / r.ks_strike * 1e4) avg_strike_diff_bps,
                      max(abs(r.pm_strike - r.ks_strike) / r.ks_strike * 1e4) max_strike_diff_bps,
                      avg(abs(r.pm_settle - r.ks_settle) / r.ks_settle * 1e4) avg_settle_diff_bps,
                      max(abs(r.pm_settle - r.ks_settle) / r.ks_settle * 1e4) max_settle_diff_bps,
                      count(*) FILTER (WHERE r.dis_kind <> 'agree') disagreements
               FROM resolutions r JOIN windows w ON w.id=r.window_id
               WHERE r.complete AND r.pm_strike IS NOT NULL AND r.ks_strike IS NOT NULL
                 AND r.pm_settle IS NOT NULL AND r.ks_settle IS NOT NULL
               GROUP BY 1 ORDER BY 1"""))
        recent = rows(await c.fetch(
            """SELECT w.asset, w.t_start, r.ks_strike, r.pm_strike, r.ks_settle, r.pm_settle,
                      r.ks_result, r.pm_result, r.dis_kind
               FROM resolutions r JOIN windows w ON w.id=r.window_id
               WHERE r.complete ORDER BY w.t_start DESC LIMIT $1""", qint(req, "limit", 40, hi=500)))
    return jr({"by_asset": agg, "recent": recent})


async def daily_overview(req):
    st = req.app["state"]
    async with req.app["pool"].acquire() as c:
        events = rows(await c.fetch(
            """SELECT e.id, e.asset, e.settle_ts, e.ks_event, e.pm_event, e.n_ks, e.n_pm, e.n_overlap, e.scanned_at,
                      (SELECT count(*) FROM daily_pairs p WHERE p.event_id=e.id AND p.active) AS active_pairs,
                      (SELECT count(*) FROM daily_trades t JOIN daily_pairs p ON p.id=t.pair_id
                        WHERE p.event_id=e.id) AS trades
               FROM daily_events e WHERE e.settle_ts > now() - interval '48 hours'
               ORDER BY e.settle_ts DESC, e.asset LIMIT 40"""))
        open_trades = await c.fetchval("SELECT count(*) FROM daily_trades WHERE status='open'")
    return jr({"enabled": req.app["cfg"].daily_enabled, "events": events, "open_trades": open_trades,
               "cycle": st.daily_stats})


async def daily_pairs_h(req):
    only_active = req.query.get("active", "1") != "0"
    async with req.app["pool"].acquire() as c:
        rs = await c.fetch(
            """SELECT p.id, p.asset, p.settle_ts, p.threshold, p.ks_ticker, p.pm_slug, p.active,
                      s.ts AS last_ts, s.status AS last_status, s.direction AS last_direction,
                      s.gross AS last_gross, s.net_bps AS last_net_bps,
                      EXISTS(SELECT 1 FROM daily_trades t WHERE t.pair_id=p.id) AS traded
               FROM daily_pairs p
               LEFT JOIN LATERAL (SELECT * FROM daily_signals x WHERE x.pair_id=p.id
                                  ORDER BY x.ts DESC LIMIT 1) s ON TRUE
               WHERE ($1::text IS NULL OR p.asset=$1) AND (NOT $2 OR p.active)
               ORDER BY p.settle_ts, p.asset, p.threshold LIMIT $3""",
            qstr(req, "asset"), only_active, qint(req, "limit", 500, hi=3000))
    return jr(rows(rs))


async def daily_signals_h(req):
    status_f = req.query.get("status")
    async with req.app["pool"].acquire() as c:
        rs = await c.fetch(
            """SELECT p.asset, p.threshold, p.settle_ts, s.* FROM daily_signals s
               JOIN daily_pairs p ON p.id=s.pair_id
               WHERE s.ts > now() - make_interval(mins => $3::int)
                 AND ($1::text IS NULL OR p.asset=$1) AND ($2::text IS NULL OR s.status=$2)
               ORDER BY s.ts DESC LIMIT $4""",
            qstr(req, "asset"), status_f.upper() if status_f else None,
            qint(req, "minutes", 60, hi=10080), qint(req, "limit", 500, hi=10000))
    return jr(rows(rs))


async def daily_trades_h(req):
    async with req.app["pool"].acquire() as c:
        rs = await c.fetch(
            """SELECT t.*, p.asset, p.threshold, p.settle_ts, (1 - t.vwap_pm - t.vwap_ks) AS gap,
                      CASE WHEN t.n > 0 THEN t.pnl / t.n END AS pnl_per_contract,
                      r.pm_result, r.ks_result, r.dis_kind
               FROM daily_trades t JOIN daily_pairs p ON p.id=t.pair_id
               LEFT JOIN daily_resolutions r ON r.pair_id=t.pair_id
               WHERE ($1::text IS NULL OR p.asset=$1) AND ($2::text IS NULL OR t.status=$2)
               ORDER BY t.ts DESC LIMIT $3""",
            qstr(req, "asset"), qstr(req, "status"), qint(req, "limit", 200, hi=5000))
    return jr(rows(rs))


async def daily_stats_h(req):
    async with req.app["pool"].acquire() as c:
        by_asset = rows(await c.fetch(
            """SELECT p.asset, count(*) n, sum(t.pnl) pnl, avg(t.pnl) mean, stddev_samp(t.pnl) sd,
                      avg((t.pnl>0)::int) win, sum(t.n*(t.vwap_pm+t.vwap_ks)+t.fees_total) capital
               FROM daily_trades t JOIN daily_pairs p ON p.id=t.pair_id
               WHERE t.status='settled' GROUP BY 1 ORDER BY 1"""))
        payouts = rows(await c.fetch(
            """SELECT (payout / n) AS payout_per_contract, count(*) n FROM daily_trades
               WHERE status='settled' GROUP BY 1 ORDER BY 1"""))
        dis = rows(await c.fetch(
            """SELECT p.asset, count(*) n, count(*) FILTER (WHERE r.dis_kind <> 'agree') disagreements
               FROM daily_resolutions r JOIN daily_pairs p ON p.id=r.pair_id
               WHERE r.complete GROUP BY 1 ORDER BY 1"""))
    return jr({"by_asset": by_asset, "payouts": payouts, "resolutions": dis})


async def logs_h(req):
    return jr(logbuf.tail(qint(req, "limit", 200, hi=3000), req.query.get("level", "INFO"),
                          req.query.get("contains"), qint(req, "after_id", 0, lo=0, hi=10**12)))


async def audit_h(req):
    async with req.app["pool"].acquire() as c:
        rs = await c.fetch("SELECT id, ts, actor, event, payload FROM audit_log ORDER BY id DESC LIMIT $1",
                           qint(req, "limit", 50, hi=500))
    out = rows(rs)
    for r in out:
        r["payload"] = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
    return jr(out)


# ---- admin ----
async def admin_protect(req):
    await maintenance.set_protected(req.app["pool"], True)
    return jr({"db_protected": True})


async def admin_unprotect(req):
    if (await body(req)).get("confirm") != "UNPROTECT":
        return jr({"error": 'envoyer {"confirm": "UNPROTECT"}'}, 400)
    await maintenance.set_protected(req.app["pool"], False)
    return jr({"db_protected": False})


async def admin_wipe(req):
    pool = req.app["pool"]
    if (await body(req)).get("confirm") != "WIPE":
        return jr({"error": 'envoyer {"confirm": "WIPE"}'}, 400)
    if await maintenance.is_protected(pool):
        return jr({"error": "DB protégée : wipe refusé"}, 409)
    await maintenance.wipe(pool, "manual via api", actor="api")
    req.app["state"].reset_caches()
    return jr({"wiped": True})


def create_app(cfg, pool, state) -> web.Application:
    app = web.Application(middlewares=[cors_mw, error_mw, auth_mw])
    app["cfg"], app["pool"], app["state"] = cfg, pool, state
    g = app.router.add_get
    g("/api/health", health); g("/api/status", status); g("/api/config", config); g("/api/live", live)
    g("/api/windows", windows_h); g("/api/quotes", quotes_h); g("/api/signals", signals_h)
    g("/api/trades", trades_h); g("/api/resolutions", resolutions_h)
    g("/api/stats/dis", stats_dis); g("/api/stats/pnl", stats_pnl); g("/api/stats/signals", stats_signals); g("/api/stats/basis", stats_basis)
    g("/api/logs", logs_h); g("/api/audit", audit_h)
    g("/api/daily/overview", daily_overview); g("/api/daily/pairs", daily_pairs_h)
    g("/api/daily/signals", daily_signals_h); g("/api/daily/trades", daily_trades_h)
    g("/api/daily/stats", daily_stats_h)
    p = app.router.add_post
    p("/api/admin/protect", admin_protect); p("/api/admin/unprotect", admin_unprotect)
    p("/api/admin/wipe", admin_wipe)
    return app


async def start(cfg, pool, state):
    if not cfg.api_id or len(cfg.api_key) < 16:
        log.warning("API DESACTIVEE : définir API_ID et API_KEY (clé >= 16 caractères)")
        return None
    runner = web.AppRunner(create_app(cfg, pool, state), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
    log.info("API en écoute sur :%s", cfg.port)
    return runner
