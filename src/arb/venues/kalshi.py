from __future__ import annotations

import logging
import time
from datetime import datetime

import httpx

from ..config import Config
from ..models import Book, MarketRef, asks_from_bids
from ..windows import KS_SERIES
from .util import parse_levels

log = logging.getLogger("arb.kalshi")


def parse_orderbook(js: dict, now: float) -> Book:
    """Kalshi renvoie des BIDS yes et no. YES = Up.
    Ask(Up)   = 1 - bid(No)    Ask(Down) = 1 - bid(Yes)
    Gère l'ancien format (cents) et le format *_dollars / orderbook_fp."""
    ob = js.get("orderbook_fp") or js.get("orderbook") or {}
    if "yes_dollars" in ob or "no_dollars" in ob:
        yes = parse_levels(ob.get("yes_dollars"), cents=False)
        no = parse_levels(ob.get("no_dollars"), cents=False)
    else:
        yes = parse_levels(ob.get("yes"), cents=True)
        no = parse_levels(ob.get("no"), cents=True)
    return Book("kalshi", asks_up=asks_from_bids(no), asks_down=asks_from_bids(yes), ts_recv=now)


def parse_result(js: dict) -> str | None:
    m = js.get("market", js)
    r = (m.get("result") or "").lower()
    return {"yes": "up", "no": "down"}.get(r)


class KalshiClient:
    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        self.cfg, self.http = cfg, http

    async def _get(self, path: str, **params) -> dict:
        r = await self.http.get(self.cfg.kalshi_base + path, params=params or None)
        r.raise_for_status()
        return r.json()

    async def discover(self, asset: str, start: datetime, end: datetime) -> MarketRef | None:
        e = int(end.timestamp())
        js = await self._get("/markets", series_ticker=KS_SERIES[asset],
                             min_close_ts=e - 1, max_close_ts=e + 1, limit=5)
        mk = js.get("markets") or []
        return MarketRef("kalshi", mk[0]["ticker"]) if mk else None

    async def fetch_book(self, ref: MarketRef) -> Book:
        js = await self._get(f"/markets/{ref.ident}/orderbook", depth=10)
        return parse_orderbook(js, time.time())

    async def fetch_result(self, ref: MarketRef) -> str | None:
        return parse_result(await self._get(f"/markets/{ref.ident}"))
