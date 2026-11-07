"""python -m arb.report  -> p̂_dis par actif/direction + résumé du paper trading."""
from __future__ import annotations

import asyncio
import math

from . import db
from .config import load
from .stats import wilson


async def run() -> None:
    cfg = load()
    pool = await db.connect(cfg.database_url)
    async with pool.acquire() as c:
        print("== Désaccords de résolution (official/official) ==")
        print(f"{'asset':5} {'n':>5} {'dis':>4} {'p̂':>7} {'IC95%':>16} {'PU_KD':>6} {'PD_KU':>6}")
        for asset, (n, k1, k2) in sorted((await db.dis_counts(pool)).items()):
            lo, hi = wilson(k1 + k2, n)
            print(f"{asset:5} {n:5d} {k1 + k2:4d} {(k1 + k2) / n:7.3%} [{lo:6.2%},{hi:6.2%}] {k1:6d} {k2:6d}")
        inferred = await c.fetchval(
            "SELECT count(*) FROM resolutions WHERE complete AND (ks_source<>'official' OR pm_source<>'official')")
        print(f"(résolutions exclues car 'inferred': {inferred})")

        print("\n== Signaux 24h ==")
        for r in await c.fetch(
                """SELECT w.asset, s.status, count(*) n, max(s.net_bps) best_bps
                   FROM signals s JOIN windows w ON w.id=s.window_id
                   WHERE s.ts > now() - interval '24 hours' GROUP BY 1,2 ORDER BY 1,2"""):
            b = r["best_bps"]
            print(f"{r['asset']:5} {r['status']:14} {r['n']:7d}  best_bps={'-' if b is None else f'{b:.0f}'}")

        print("\n== Paper trades réglés ==")
        for r in await c.fetch(
                """SELECT w.asset, count(*) n, sum(pnl) pnl, avg(pnl) mean, stddev_samp(pnl) sd,
                          avg((pnl>0)::int) win, sum(n*(vwap_pm+vwap_ks)+fees_total) cap
                   FROM paper_trades p JOIN windows w ON w.id=p.window_id
                   WHERE p.status='settled' GROUP BY 1 ORDER BY 1"""):
            sd = r["sd"] or float("nan")
            sharpe = r["mean"] / sd if sd and not math.isnan(sd) and sd > 0 else float("nan")
            print(f"{r['asset']:5} n={r['n']:4d} pnl=${r['pnl']:8.2f} mean=${r['mean']:6.3f} "
                  f"sd=${sd:6.3f} win={r['win']:.0%} per-trade-sharpe={sharpe:5.2f} roc={r['pnl'] / r['cap']:.2%}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(run())
