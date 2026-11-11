"""Aperçu du scan « pluie par ville », SANS base de données :

    PYTHONPATH=src python scripts/probe_weather.py [--raw]

Affiche : marchés Kalshi KXRAIN lus (ville, jour, stations, règle des traces), marchés Polymarket INTERNATIONAL
trouvés, couples communs avec verdict (négociable ou non, et pourquoi), villes présentes d'un seul côté.
--raw : JSON brut du premier événement météo Polymarket (pour vérifier les champs).
"""
import asyncio
import json
import sys
from datetime import datetime, timezone

import httpx

from arb.config import load
from arb.venues.kalshi import KalshiClient
from arb.venues.polymarket_intl import PolymarketIntl
from arb.weather import gather_weather
from arb.weather_core import parse_intl_rain_event


async def main(raw: bool) -> None:
    cfg = load()
    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=15) as http:
        kal, pmi = KalshiClient(cfg, http), PolymarketIntl(cfg, http)
        res = await gather_weather(cfg, kal, pmi, now)
        print(f"Kalshi KXRAIN : {res['ks_n']} (ville, jour) | Polymarket intl : {res['pm_n']} "
              f"(sur {res['pm_events']} événements météo) | communs retenus : {len(res['pairs'])}")
        print("\n  Villes  | Kalshi (jours) | Polymarket intl (jours) | communs")
        for c, v in res["cities"].items():
            print(f"  {c:7} | {','.join(d[5:] for d in v['kalshi_days']) or '-':14} | "
                  f"{','.join(d[5:] for d in v['polymarket_days']) or '-':23} | {','.join(d[5:] for d in v['common_days']) or '-'}")
        print("  villes NON reconnues  kalshi:", res["unknown_ks"][:15], " polymarket:", res["unknown_pm"][:15], "\n")
        for p in res["pairs"]:
            verdict = f"OK direction={p['safe_direction']}" if p["tradable"] else f"NON : {p['reason']}"
            print(f"  {p['city']} {p['day']}  station={p['station_status']} (ks={p['ks_station'] or '-'} "
                  f"pm={p['pm_station'] or '-'})  traces ks={p['ks_trace']} pm={p['pm_trace']}  -> {verdict}")
        print("  seulement Kalshi    :", res["only_ks"][:20])
        print("  seulement Polymarket:", res["only_pm"][:20])
        if raw:
            evs = await pmi.weather_events(max_pages=1)
            rain = [e for e in evs if parse_intl_rain_event(e)]
            print("\n== premier événement pluie Polymarket (JSON tronqué) ==")
            print(json.dumps(rain[0] if rain else (evs[0] if evs else {}), indent=1)[:4000])


if __name__ == "__main__":
    asyncio.run(main("--raw" in sys.argv))
