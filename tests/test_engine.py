import pytest

from arb.config import Config
from arb.engine import dis_kind, evaluate, kalshi_fee, payout, vwap
from arb.models import Book, asks_from_bids
from arb.stats import DisEstimate, estimate, wilson

NOW = 1_000.0
ZERO = DisEstimate(p_dis=0.0, asym=0.0, n=0, source="test")


def book(venue, up, down, ts=NOW):
    return Book(venue, asks_up=up, asks_down=down, ts_recv=ts)


def test_vwap_flat_and_two_levels_and_depth():
    assert vwap([(0.5, 100)], 10) == pytest.approx(0.5)
    assert vwap([(0.40, 5), (0.50, 10)], 10) == pytest.approx((5 * 0.40 + 5 * 0.50) / 10)
    assert vwap([(0.5, 5)], 10) is None


def test_asks_from_bids():
    assert asks_from_bids([(0.60, 3), (0.55, 4)]) == [(0.4, 3), (0.45, 4)]


@pytest.mark.parametrize("p,n,exp", [(0.5, 1, 0.02), (0.47, 10, 0.18), (0.01, 1, 0.01),
                                     (0.99, 1, 0.01), (0.5, 100, 1.75)])
def test_kalshi_fee(p, n, exp):
    assert kalshi_fee(p, n) == pytest.approx(exp)


def test_payout_and_dis_kind():
    assert payout("PU_KD", "up", "up") == 1 and payout("PU_KD", "down", "down") == 1
    assert payout("PU_KD", "up", "down") == 2 and payout("PU_KD", "down", "up") == 0
    assert payout("PD_KU", "down", "up") == 2 and payout("PD_KU", "up", "down") == 0
    assert dis_kind("up", "up") == "agree" and dis_kind("up", "down") == "PU_KD"
    assert dis_kind("down", "up") == "PD_KU"


def cfg(**kw):
    return Config(pm_fee_coef=0.0, pm_intl_fee_coef=0.0, ks_fee_coef=0.0, **kw)


def test_actionable_when_edge_fresh_deep():
    pm = book("polymarket", [(0.45, 100)], [(0.58, 100)])
    ks = book("kalshi", [(0.57, 100)], [(0.50, 100)])
    ev = evaluate(pm, ks, 10, 400, NOW, cfg(), ZERO)   # PU_KD : 0.45+0.50 = 0.95
    assert ev.direction == "PU_KD" and ev.gross == pytest.approx(0.05)
    assert ev.status == "ACTIONABLE" and ev.net == pytest.approx(0.05)


def test_stale_rejected_fresh_allowed():
    pm = book("polymarket", [(0.45, 100)], [(0.58, 100)], ts=NOW - 3)
    ks = book("kalshi", [(0.57, 100)], [(0.50, 100)], ts=NOW - 3)
    assert evaluate(pm, ks, 10, 400, NOW, cfg(), ZERO).status == "REJECT_STALE"
    pm.ts_recv = ks.ts_recv = NOW - 1
    assert evaluate(pm, ks, 10, 400, NOW, cfg(), ZERO).status == "ACTIONABLE"


def test_depth_rejected():
    pm = book("polymarket", [(0.45, 2)], [(0.58, 2)])
    ks = book("kalshi", [(0.57, 2)], [(0.50, 2)])
    assert evaluate(pm, ks, 10, 400, NOW, cfg(), ZERO).status == "REJECT_DEPTH"


def test_below_threshold_is_observe_and_last_window_regime():
    pm = book("polymarket", [(0.50, 100)], [(0.52, 100)])
    ks = book("kalshi", [(0.51, 100)], [(0.50, 100)])   # gross 0
    assert evaluate(pm, ks, 10, 400, NOW, cfg(), ZERO).status == "OBSERVE"
    pm = book("polymarket", [(0.45, 100)], [(0.58, 100)])
    ks = book("kalshi", [(0.57, 100)], [(0.50, 100)])
    ev = evaluate(pm, ks, 10, 30, NOW, cfg(), ZERO)
    assert ev.status == "OBSERVE" and ev.reason == "last_window_regime"


def test_fees_reduce_edge():
    pm = book("polymarket", [(0.48, 100)], [(0.58, 100)])
    ks = book("kalshi", [(0.57, 100)], [(0.50, 100)])
    ev = evaluate(pm, ks, 10, 400, NOW, Config(pm_fee_coef=0.0, pm_intl_fee_coef=0.0, ks_fee_coef=0.07), ZERO)
    assert ev.fees_total == pytest.approx(kalshi_fee(0.50, 10))
    assert ev.net == pytest.approx(1 - 0.98 - ev.fees_total / 10)


def test_asymmetry_favours_one_direction():
    pm = book("polymarket", [(0.50, 100)], [(0.50, 100)])
    ks = book("kalshi", [(0.50, 100)], [(0.50, 100)])
    ev = evaluate(pm, ks, 10, 400, NOW, cfg(), DisEstimate(0.1, 0.02, 100, "t"))
    assert ev.direction == "PU_KD" and ev.net == pytest.approx(0.02)


def test_haircut_and_lambda():
    pm = book("polymarket", [(0.45, 100)], [(0.58, 100)])
    ks = book("kalshi", [(0.57, 100)], [(0.50, 100)])
    d = DisEstimate(0.04, 0.0, 100, "t")
    ev = evaluate(pm, ks, 10, 400, NOW, cfg(dis_haircut=1.0), d)
    assert ev.net == pytest.approx(0.05 - 0.04 * 0.95)


def test_stats():
    e = estimate(0, 0, 0, 0.10, 50)
    assert e.p_dis == pytest.approx(0.10) and e.asym == 0
    e = estimate(100, 6, 2, 0.10, 50)
    assert e.p_dis == pytest.approx((8 + 5) / 150) and e.asym == pytest.approx(4 / 150)
    lo, hi = wilson(8, 100)
    assert lo < 0.08 < hi
