"""Aperçu du scan des marchés journaliers, SANS base de données :

    PYTHONPATH=src python scripts/probe_daily.py [btc|eth|sol|xrp]

Affiche, pour l'actif : événements Kalshi trouvés, événements Polymarket lus, seuils communs et notes de
diagnostic. Si 'communs' vaut 0, colle la sortie complète (et `--raw` pour le JSON brut Polymarket).
"""
import asyncio
import json
import sys
from datetime import datetime, timezone

import httpx

from arb.config import load
from arb.daily import gather_asset
from arb.daily_core import NY, pm_daily_slug
from arb.venues.kalshi import KalshiClient
from arb.venues.polymarket import PolymarketClient


async def main(asset: str, raw: bool) -> None:
    cfg = load()
    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=10) as http:
        kal, pmc = KalshiClient(cfg, http), PolymarketClient(cfg, http)
        res = await gather_asset(cfg, kal, pmc, asset, now)
        print(f"== {asset} : événements Kalshi ouverts ==")
        for ev, d in sorted(res["ks_events"].items(), key=lambda x: x[1]["settle"]):
            print(f"  {ev:24} règlement {d['settle']:%Y-%m-%d %H:%MZ}  marchés={d['n']}")
        print(f"  (marchés Kalshi ignorés faute de seuil/heure lisible : {res['ks_skipped']})")
        print(f"\n== {asset} : correspondances ==")
        for m in res["matches"]:
            print(f"  {m['pm_event']} <-> {m['ks_event']}  règlement {m['settle']:%Y-%m-%d %H:%MZ}  "
                  f"kalshi={m['n_ks']} polymarket={m['n_pm']} communs={len(m['pairs'])}")
            for th, k, p in m["pairs"][:6]:
                print(f"     seuil {th:>12}  {k}  {p}")
        for n in res["notes"]:
            print("  NOTE:", n)
        if raw:
            slug = pm_daily_slug(asset, now.astimezone(NY).date())
            print(f"\n== JSON brut Polymarket {slug} (tronqué) ==")
            print(json.dumps(await pmc.fetch_event(slug), indent=1)[:4000])


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    asyncio.run(main(args[0] if args else "btc", "--raw" in sys.argv))
