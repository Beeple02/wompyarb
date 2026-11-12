from __future__ import annotations

import json
import logging
import time
from datetime import datetime

import httpx

from ..config import Config
from ..models import Book, MarketRef, asks_from_bids
from ..windows import pm_event_slug
from .util import find_key, num, parse_levels

log = logging.getLogger("arb.polymarket")

_MAP = {"up": "up", "yes": "up", "long": "up", "down": "down", "no": "down", "short": "down"}


def parse_book(js: dict, now: float, book_is_up: bool = True) -> Book:
    """Le book public est celui d'UN outcome (par défaut Up/long).
    Ask(Up) = asks ; Ask(Down) = 1 - bids.  PM_BOOK_IS_UP=false inverse."""
    bids = parse_levels(find_key(js, ("bids", "bid")))
    asks = sorted(parse_levels(find_key(js, ("asks", "offers", "ask"))))
    up_asks, down_asks = asks, asks_from_bids(bids)
    if not book_is_up:
        up_asks, down_asks = down_asks, up_asks
    return Book("polymarket", asks_up=up_asks, asks_down=down_asks, ts_recv=now)


def parse_result(js: dict) -> str | None:
    """Tolérant : cherche un gagnant explicite, sinon outcomePrices d'un marché clos."""
    w = find_key(js, ("winner", "winningOutcome", "winning_outcome", "resolvedOutcome",
                      "resolved_outcome", "resolution"))
    if isinstance(w, str) and w.lower() in _MAP:
        return _MAP[w.lower()]
    closed = find_key(js, ("closed", "resolved", "ended"))
    outcomes, prices = find_key(js, ("outcomes",)), find_key(js, ("outcomePrices", "outcome_prices"))
    if closed is True and outcomes and prices:
        try:
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            prices = json.loads(prices) if isinstance(prices, str) else prices
            for name, p in zip(outcomes, prices):
                if float(p) >= 0.99 and str(name).lower() in _MAP:
                    return _MAP[str(name).lower()]
        except (ValueError, TypeError):
            return None
    return None


def _json_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def parse_market(m: dict) -> dict:
    """Statut, résultat (Yes = Up/above) et valeurs de référence d'UN marché (objet markets[i])."""
    status = str(m.get("status", ""))
    terms = m.get("assetPriceTerms") or {}
    result = None
    outcomes, prices = _json_list(m.get("outcomes")), _json_list(m.get("outcomePrices"))
    if "RESOLVED" in status.upper() and outcomes and prices:
        try:
            for name, p in zip(outcomes, prices):
                if float(p) >= 0.99 and str(name).lower() in _MAP:
                    result = _MAP[str(name).lower()]
        except (TypeError, ValueError):
            result = None
    return {"result": result, "strike": num(terms.get("priceToBeat")),
            "settle": num(terms.get("settlementPrice")), "status": status}


def parse_event(js: dict) -> dict | None:
    """Résultat d'un événement up/down Polymarket US (endpoint /v1/events/slug/{slug}).

    Structure observée : event.markets[0] avec status MARKET_STATUS_RESOLVED, outcomes ["Yes","No"],
    outcomePrices ["0","1"] (Yes = Up = côté long) et assetPriceTerms.{priceToBeat, settlementPrice}.
    Le résultat n'est renvoyé qu'une fois le marché résolu."""
    ev = js.get("event", js) if isinstance(js, dict) else {}
    markets = ev.get("markets") if isinstance(ev, dict) else None
    m = markets[0] if isinstance(markets, list) and markets and isinstance(markets[0], dict) else None
    return parse_market(m) if m is not None else None


_DIAG_HINTS = ("active", "closed", "archived", "state", "status", "tradable", "accepting", "orderbook",
               "enable", "open", "start", "end", "resolved", "liquidity", "volume")


class PolymarketClient:
    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        self.cfg, self.http = cfg, http

    async def _get(self, path_tpl: str, slug: str) -> dict:
        r = await self.http.get(self.cfg.pm_base + path_tpl.format(slug=slug))
        r.raise_for_status()
        return r.json()

    async def discover(self, asset: str, start: datetime, end: datetime) -> MarketRef | None:
        ev_slug = pm_event_slug(asset, start)
        try:
            js = await self._get(self.cfg.pm_event_path, ev_slug)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        markets = find_key(js, ("markets",))
        if isinstance(markets, list):
            for m in markets:
                if isinstance(m, dict) and m.get("slug"):
                    return MarketRef("polymarket", m["slug"])
        return MarketRef("polymarket", ev_slug)

    async def fetch_book(self, ref: MarketRef) -> Book:
        js = await self._get(self.cfg.pm_book_path, ref.ident)
        return parse_book(js, time.time(), self.cfg.pm_book_is_up)

    async def fetch_result(self, ref: MarketRef) -> str | None:
        return parse_result(await self._get(self.cfg.pm_market_path, ref.ident))

    async def fetch_event(self, slug: str) -> dict:
        """JSON brut d'un événement (marchés journaliers : un événement = une échelle de seuils)."""
        return await self._get(self.cfg.pm_event_path, slug)

    async def fetch_event_result(self, asset: str, start: datetime) -> dict | None:
        """Résultat via l'endpoint ÉVÉNEMENT (le endpoint marché /v1/markets/slug/.. renvoie 404 après
        la clôture ; l'événement reste disponible et porte le résultat)."""
        return parse_event(await self._get(self.cfg.pm_event_path, pm_event_slug(asset, start)))

    async def diagnose(self, ref: MarketRef) -> str:
        """Résumé (champs scalaires pertinents) du marché, pour comprendre un 404 sur le book."""
        try:
            js = await self._get(self.cfg.pm_market_path, ref.ident)
            m = js.get("market", js) if isinstance(js, dict) else {}
            keep = {k: v for k, v in m.items()
                    if any(h in k.lower() for h in _DIAG_HINTS) and not isinstance(v, (dict, list))}
            return "market=" + json.dumps(keep, default=str)[:600]
        except Exception as e:  # diagnostic seulement : ne doit jamais casser le tick
            return f"market_fetch_error={e!r}"[:200]
