"""Teste les endpoints en vrai et affiche le JSON brut + ce que le parser en tire.

    PYTHONPATH=src python scripts/probe.py [btc|eth|sol|xrp]

A lancer AVANT le premier déploiement. Si Polymarket US renvoie 404/autre forme,
ajuste PM_EVENT_PATH / PM_MARKET_PATH / PM_BOOK_PATH (et PM_BOOK_IS_UP) dans .env.
"""
import asyncio
import json
import sys
from datetime import datetime, timezone

import httpx

from arb import windows
from arb.config import load
from arb.venues import kalshi, polymarket
from arb.venues.kalshi import KalshiClient
from arb.venues.polymarket import PolymarketClient


def show(title, obj, n=1500):
    s = json.dumps(obj, indent=2, default=str) if not isinstance(obj, str) else obj
    print(f"\n--- {title} ---\n{s[:n]}{'...' if len(s) > n else ''}")


async def main(asset: str):
    cfg = load()
    start, end = windows.current(datetime.now(timezone.utc))
    print(f"asset={asset} window {start} -> {end}  pm_event={windows.pm_event_slug(asset, start)}")
    async with httpx.AsyncClient(timeout=8) as http:
        kal, pmc = KalshiClient(cfg, http), PolymarketClient(cfg, http)
        try:
            ref = await kal.discover(asset, start, end)
            print("Kalshi ref:", ref)
            raw = (await http.get(f"{cfg.kalshi_base}/markets/{ref.ident}/orderbook", params={"depth": 5})).json()
            show("Kalshi orderbook RAW", raw)
            b = kalshi.parse_orderbook(raw, 0)
            print("-> parsed asks_up", b.asks_up[:3], "asks_down", b.asks_down[:3])
            show("Kalshi market RAW", (await http.get(f"{cfg.kalshi_base}/markets/{ref.ident}")).json(), 1200)
        except Exception as e:
            print("KALSHI ERREUR:", repr(e))
        try:
            slug = windows.pm_event_slug(asset, start)
            for tpl in (cfg.pm_event_path, cfg.pm_market_path):
                r = await http.get(cfg.pm_base + tpl.format(slug=slug))
                print(f"\nGET {tpl.format(slug=slug)} -> {r.status_code}")
                if r.status_code == 200:
                    show("Polymarket RAW", r.json(), 2500)
            ref = await pmc.discover(asset, start, end)
            print("PM ref:", ref)
            r = await http.get(cfg.pm_base + cfg.pm_book_path.format(slug=ref.ident))
            print(f"GET book -> {r.status_code}")
            if r.status_code == 200:
                show("Polymarket book RAW", r.json())
                b = polymarket.parse_book(r.json(), 0, cfg.pm_book_is_up)
                print("-> parsed asks_up", b.asks_up[:3], "asks_down", b.asks_down[:3])
                print("VERIFIE que 'up' = Up (sinon PM_BOOK_IS_UP=false)")
        except Exception as e:
            print("POLYMARKET ERREUR:", repr(e))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "btc"))
