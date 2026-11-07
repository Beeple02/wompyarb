from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class DisEstimate:
    p_dis: float   # P(les deux venues résolvent différemment)
    asym: float    # P(PM up, KS down) - P(PM down, KS up), shrunk vers 0
    n: int
    source: str


def estimate(n: int, k_pu_kd: int, k_pd_ku: int, prior: float, strength: float) -> DisEstimate:
    k = k_pu_kd + k_pd_ku
    p = (k + prior * strength) / (n + strength)
    asym = (k_pu_kd - k_pd_ku) / (n + strength)
    return DisEstimate(p_dis=p, asym=asym, n=n, source="empirical+prior" if n else "prior")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)
