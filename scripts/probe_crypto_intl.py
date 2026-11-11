"""Vérifie le moteur 15 min sur Polymarket INTERNATIONAL pour la fenêtre en cours, SANS base de données :

    PYTHONPATH=src python scripts/probe_crypto_intl.py [btc|eth|sol|xrp]

Affiche le slug (epoch de début de fenêtre), le token Up, le meilleur ask/bid du CLOB, le taux de frais renvoyé
par /fee-rate, et le résultat Gamma de la fenêtre précédente. À lancer avant de se fier aux écarts.
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

import httpx

from arb import windows
from arb.config import load
from arb.venues.polymarket_intl import PolymarketIntlCrypto


async def main(asset: str) -> None:
    cfg = load()
    now = datetime.now(timezone.utc)
    start, end = windows.current(now)
    async with httpx.AsyncClient(timeout=10) as http:
        pm = PolymarketIntlCrypto(cfg, http)
        print(f"fenêtre {start:%H:%M}Z-{end:%H:%M}Z  slug={pm.slug(asset, start)}")
        ref = await pm.discover(asset, start, end)
        print("token Up :", ref and ref.ident)
        if ref:
            book = await pm.fetch_book(ref)
            print("ask Up / ask Down :", book.best("up"), "/", book.best("down"), " profondeur :",
                  book.depth("up"), "/", book.depth("down"))
            print("frais :", await pm.diagnose(ref))
        prev = start - timedelta(minutes=15)
        print("résultat fenêtre précédente :", await pm.fetch_event_result(asset, prev))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "btc"))
