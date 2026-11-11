from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config
from .models import Book, Level
from .stats import DisEstimate
from .windows import WINDOW_S


def kalshi_fee(p: float, n: float, coef: float = 0.07) -> float:
    """Total fee in dollars: ceil(coef * n * p * (1-p)) arrondi au cent supérieur."""
    raw_cents = coef * n * p * (1 - p) * 100
    return math.ceil(round(raw_cents, 9)) / 100


def pm_fee(p: float, n: float, coef: float) -> float:
    """Placeholder : même forme que Kalshi, SANS arrondi. A remplacer par la vraie grille."""
    return coef * n * p * (1 - p)


def vwap(levels: list[Level], n: float) -> float | None:
    """Prix moyen pour acheter n contrats, None si profondeur insuffisante."""
    if n <= 0:
        return None
    need, cost = n, 0.0
    for price, qty in levels:
        take = min(need, qty)
        cost += take * price
        need -= take
        if need <= 1e-9:
            return cost / n
    return None


DIRECTIONS = {
    "PU_KD": ("up", "down"),   # achète Up sur Polymarket, Down (NO) sur Kalshi
    "PD_KU": ("down", "up"),
}


def payout(direction: str, pm_result: str, ks_result: str) -> int:
    pm_side, ks_side = DIRECTIONS[direction]
    return int(pm_result == pm_side) + int(ks_result == ks_side)


def dis_kind(pm_result: str, ks_result: str) -> str:
    if pm_result == ks_result:
        return "agree"
    return "PU_KD" if pm_result == "up" else "PD_KU"


@dataclass
class Evaluation:
    status: str                      # REJECT_STALE | REJECT_DEPTH | OBSERVE | ACTIONABLE
    reason: str = ""
    direction: str | None = None
    n: float = 0
    vwap_pm: float | None = None
    vwap_ks: float | None = None
    fees_total: float = 0.0
    cost_per: float = 0.0            # coût par contrat, fees incluses
    gross: float = 0.0               # 1 - (a + b)
    net: float = 0.0                 # espérance nette par contrat
    net_bps: float = 0.0             # net / cost_per, en bps
    p_dis: float = 0.0
    payout_std: float = 0.0
    slippage: float = 0.0            # vwap - top-of-book (info, déjà inclus dans gross)


def evaluate(pm: Book, ks: Book, n: float, secs_to_close: float, now_ts: float,
             cfg: Config, dis: DisEstimate) -> Evaluation:
    if (now_ts - pm.ts_recv > cfg.max_stale_s or now_ts - ks.ts_recv > cfg.max_stale_s
            or abs(pm.ts_recv - ks.ts_recv) > cfg.max_skew_s):
        return Evaluation("REJECT_STALE", "stale_or_skew", p_dis=dis.p_dis)

    best: Evaluation | None = None
    for direction, (pm_side, ks_side) in DIRECTIONS.items():
        pm_lad = pm.asks_up if pm_side == "up" else pm.asks_down
        ks_lad = ks.asks_up if ks_side == "up" else ks.asks_down
        a, b = vwap(pm_lad, n), vwap(ks_lad, n)
        if a is None or b is None:
            continue
        fees = pm_fee(a, n, cfg.pm_fee_eff) + kalshi_fee(b, n, cfg.ks_fee_coef)
        cost_per = a + b + fees / n
        gross = 1.0 - (a + b)
        # Si les désaccords sont asymétriques, une direction a une espérance > 1, l'autre < 1.
        asym_adj = dis.asym if direction == "PU_KD" else -dis.asym
        std = math.sqrt(max(dis.p_dis, 0.0))
        net = (1.0 - cost_per) + asym_adj - cfg.dis_haircut * dis.p_dis * cost_per - cfg.lam * std
        slip = (a - pm.best(pm_side)) + (b - ks.best(ks_side))
        ev = Evaluation("OBSERVE", direction=direction, n=n, vwap_pm=a, vwap_ks=b,
                        fees_total=fees, cost_per=cost_per, gross=gross, net=net,
                        net_bps=net / cost_per * 1e4, p_dis=dis.p_dis, payout_std=std,
                        slippage=slip)
        if best is None or ev.net > best.net:
            best = ev
    if best is None:
        return Evaluation("REJECT_DEPTH", "depth<n", p_dis=dis.p_dis)

    since_open = WINDOW_S - secs_to_close
    if best.net_bps >= cfg.min_edge_bps:
        if secs_to_close < cfg.no_entry_last_s:
            best.reason = "last_window_regime"
        elif since_open < cfg.no_entry_first_s:
            best.reason = "window_just_opened"
        else:
            best.status = "ACTIONABLE"
    return best
