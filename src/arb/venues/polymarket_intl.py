"""Polymarket INTERNATIONAL (polymarket.com) : API publique sans clé.

  Gamma  https://gamma-api.polymarket.com  : événements / marchés (clobTokenIds, outcomes... en chaînes JSON)
  CLOB   https://clob.polymarket.com       : GET /book?token_id=<id>  (bids croissants, asks décroissants)

Distinct de Polymarket US (gateway.polymarket.us) : autres marchés, autres carnets, autres liquidités.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from ..config import Config
from ..models import Book, MarketRef
from .polymarket import _MAP, parse_book

log = logging.getLogger("arb.polymarket_intl")


def _jl(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def parse_intl_result(m: dict) -> dict:
    """Marché Gamma -> {result: 'up' (Yes) | 'down' (No) | None, closed}. Résultat seulement si clos et tranché."""
    def jl(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except ValueError:
                return None
        return v
    outcomes, prices = jl(m.get("outcomes")), jl(m.get("outcomePrices"))
    result = None
    resolved = bool(m.get("closed")) or str(m.get("umaResolutionStatus") or "").lower() == "resolved"
    if resolved and outcomes and prices:
        try:
            for name, p in zip(outcomes, prices):
                if float(p) >= 0.99 and str(name).lower() in _MAP:
                    result = _MAP[str(name).lower()]
        except (TypeError, ValueError):
            result = None
    return {"result": result, "closed": bool(m.get("closed")),
            "uma": m.get("umaResolutionStatus")}


class PolymarketIntl:
    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        self.cfg, self.http = cfg, http

    async def _get(self, base: str, path: str, **params):
        for attempt in range(3):
            r = await self.http.get(base + path, params=params or None)
            if r.status_code == 429 and attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")

    async def events(self, **params) -> list[dict]:
        js = await self._get(self.cfg.pm_gamma_base, "/events", **params)
        return js if isinstance(js, list) else (js.get("events") or [])

    async def weather_events(self, max_pages: int = 6) -> list[dict]:
        """Événements météo ouverts (catégorie 'weather'), paginés."""
        out: list[dict] = []
        for page in range(max_pages):
            batch = await self.events(active="true", closed="false", tag_slug="weather", limit=100,
                                      offset=page * 100, order="endDate", ascending="true")
            out += batch
            if len(batch) < 100:
                break
        return out

    async def search_events(self, q: str) -> list[dict]:
        js = await self._get(self.cfg.pm_gamma_base, "/public-search", q=q)
        return js.get("events") or [] if isinstance(js, dict) else []

    async def fetch_book(self, ref: MarketRef) -> Book:
        """ref.ident = token_id du côté Yes. Ask(Yes) = asks ; Ask(No) = 1 - bids."""
        js = await self._get(self.cfg.pm_clob_base, "/book", token_id=ref.ident)
        return parse_book(js, time.time(), True)

    async def fetch_market(self, slug: str) -> dict | None:
        """Marché Gamma par slug, OUVERT ou CLOS.

        `/markets?slug=X` ne renvoie que les marchés encore ouverts : dès la clôture il répond `[]`.
        Sans le second appel (`closed=true`), le résultat d'un marché 15 min n'est jamais trouvé, la
        résolution reste incomplète et les paper trades ne sont jamais réglés."""
        for params in ({"slug": slug}, {"slug": slug, "closed": "true"}):
            js = await self._get(self.cfg.pm_gamma_base, "/markets", **params)
            if isinstance(js, list):
                if js:
                    return js[0]
            elif isinstance(js, dict) and js:
                return js
        return None


class PolymarketIntlCrypto(PolymarketIntl):
    """Marchés crypto up/down de 15 min sur Polymarket INTERNATIONAL, avec la même interface que le client US.

    Identifiant : `{asset}-updown-15m-{epoch du DÉBUT de fenêtre}` (ex. btc-updown-15m-1776453300 = 17 avr 15h15 ET).
    Résolution : flux Chainlink (point dans le temps), différente de Kalshi (moyenne BRTI sur 60 s) : un désaccord
    de résolution est possible, mesuré par `resolutions.dis_kind`.
    """

    @staticmethod
    def slug(asset: str, start) -> str:
        return f"{asset}-updown-15m-{int(start.timestamp())}"

    async def discover(self, asset: str, start, end) -> MarketRef | None:
        slug = self.slug(asset, start)
        evs = await self.events(slug=slug)
        for ev in evs:
            for m in ev.get("markets") or []:
                outcomes, tokens = _jl(m.get("outcomes")), _jl(m.get("clobTokenIds"))
                if not outcomes or not tokens or len(outcomes) != len(tokens):
                    continue
                idx = next((i for i, o in enumerate(outcomes) if str(o).lower() in ("up", "yes")), None)
                if idx is not None:
                    return MarketRef("polymarket", str(tokens[idx]), slug=m.get("slug") or slug)
        return None

    async def diagnose(self, ref: MarketRef) -> str:
        try:
            fee = await self._get(self.cfg.pm_clob_base, "/fee-rate", token_id=ref.ident)
            return f"fee-rate={fee}"
        except Exception as e:
            return f"diagnose_error={e!r}"[:200]

    async def fetch_event_result(self, asset: str, start) -> dict | None:
        slug = self.slug(asset, start)
        m = await self.fetch_market(slug)
        if not m:
            # filet : l'événement porte aussi ses marchés (même slug), y compris après clôture
            for ev in await self.events(slug=slug):
                m = next((x for x in (ev.get("markets") or [])
                          if isinstance(x, dict) and (x.get("slug") or slug) == slug), None)
                if m:
                    break
        if not m:
            return None
        r = parse_intl_result(m)
        return {"result": r["result"], "strike": None, "settle": None,
                "status": "closed" if r["closed"] else "open"}
