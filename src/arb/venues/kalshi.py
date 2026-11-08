from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import httpx

from ..config import Config
from ..models import Book, MarketRef, asks_from_bids
from ..windows import KS_SERIES
from .util import num, parse_levels

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


def parse_market_result(js: dict) -> dict:
    """Résultat + prix de référence (floor_strike) et valeur de règlement (expiration_value) si présents."""
    m = js.get("market", js)
    return {"result": parse_result(js), "strike": num(m.get("floor_strike")),
            "settle": num(m.get("expiration_value"))}


class KalshiClient:
    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        self.cfg, self.http = cfg, http

    async def _get(self, path: str, **params) -> dict:
        for attempt in range(3):
            r = await self.http.get(self.cfg.kalshi_base + path, params=params or None)
            if r.status_code == 429 and attempt < 2:   # trop de requêtes : on attend puis on réessaie
                try:
                    wait = float(r.headers.get("Retry-After", 1.0))
                except ValueError:
                    wait = 1.0
                await asyncio.sleep(min(wait, 5.0) * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")

    async def list_markets(self, series: str, status: str = "open", max_pages: int = 5) -> list[dict]:
        """Tous les marchés ouverts d'une série (pagination par curseur)."""
        out: list[dict] = []
        cursor = None
        for _ in range(max_pages):
            params = {"series_ticker": series, "status": status, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            js = await self._get("/markets", **params)
            out += js.get("markets") or []
            cursor = js.get("cursor")
            if not cursor:
                break
        return out

    async def discover(self, asset: str, start: datetime, end: datetime) -> MarketRef | None:
        e = int(end.timestamp())
        js = await self._get("/markets", series_ticker=KS_SERIES[asset],
                             min_close_ts=e - 1, max_close_ts=e + 1, limit=5)
        mk = js.get("markets") or []
        return MarketRef("kalshi", mk[0]["ticker"]) if mk else None

    async def fetch_book(self, ref: MarketRef) -> Book:
        js = await self._get(f"/markets/{ref.ident}/orderbook", depth=10)
        return parse_orderbook(js, time.time())

    async def fetch_result(self, ref: MarketRef) -> dict:
        return parse_market_result(await self._get(f"/markets/{ref.ident}"))
