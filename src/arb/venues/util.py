from __future__ import annotations

import json
from typing import Any, Iterable

from ..models import Level


def num(v: Any) -> float | None:
    """float depuis nombre, string, ou dict {'value': '0.52'}."""
    if v is None:
        return None
    if isinstance(v, dict):
        for k in ("value", "amount", "price", "px"):
            if k in v:
                return num(v[k])
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def find_key(obj: Any, names: Iterable[str]) -> Any:
    """Recherche en largeur de la première clé dont le nom (insensible à la casse) est dans names."""
    wanted = {n.lower() for n in names}
    queue = [obj]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k.lower() in wanted and v is not None:
                    return v
            queue.extend(cur.values())
        elif isinstance(cur, list):
            queue.extend(cur)
    return None


def parse_levels(raw: Any, cents: bool | None = None) -> list[Level]:
    """Accepte [[p,q],...] ou [{'px':..,'qty':..},...]. cents=None => auto (p>1 => cents)."""
    out: list[Level] = []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return out
    for it in raw or []:
        if isinstance(it, dict):
            p = num(it.get("px", it.get("price", it.get("p"))))
            q = num(it.get("qty", it.get("quantity", it.get("size", it.get("sz", it.get("q"))))))
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            p, q = num(it[0]), num(it[1])
        else:
            continue
        if p is None or q is None or q <= 0:
            continue
        if cents is True or (cents is None and p > 1.0):
            p /= 100.0
        if 0.0 < p < 1.0:
            out.append((round(p, 6), q))
    return out
