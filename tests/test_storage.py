import asyncio

from arb.storage import Counters, plan, should_store


def test_should_store_policy():
    # ACTIONABLE / UNVERIFIED : toujours
    assert should_store("ACTIONABLE", 120, 100.0, 101.0, 30, 10)
    assert should_store("UNVERIFIED", 90, 100.0, 101.0, 30, 10)
    # REJECT_* : jamais ligne par ligne (comptés dans signal_counts)
    assert not should_store("REJECT_DEPTH", None, None, 101.0, 30, 10)
    assert not should_store("REJECT_STALE", None, 0.0, 1e9, 30, 10)
    # météo / journalier : une trace d'un book vide ou trop fin toutes les 10 min
    assert should_store("REJECT_DEPTH", None, None, 100.0, 120, 10, reject_every_s=600)
    assert not should_store("REJECT_DEPTH", None, 100.0, 400.0, 120, 10, reject_every_s=600)
    assert should_store("REJECT_DEPTH", None, 100.0, 701.0, 120, 10, reject_every_s=600)
    # OBSERVE : échantillonné toutes les N secondes
    assert should_store("OBSERVE", -200, None, 100.0, 30, 10)            # jamais stocké : on stocke
    assert not should_store("OBSERVE", -200, 100.0, 110.0, 30, 10)       # 10 s < 30 s
    assert should_store("OBSERVE", -200, 100.0, 131.0, 30, 10)
    # quasi-opportunité : stockée même hors cadence
    assert should_store("OBSERVE", 15, 100.0, 101.0, 30, 10)


def test_plan_thresholds():
    assert plan(100, 500) == "ok"
    assert plan(349, 500) == "ok" and plan(350, 500) == "prune"
    assert plan(449, 500) == "prune" and plan(450, 500) == "truncate"


def test_counters_aggregate_and_drain():
    c = Counters()
    t = 1_700_000_000.0
    c.add("crypto15", "btc", "OBSERVE", -100.0, ts=t)
    c.add("crypto15", "btc", "OBSERVE", 40.0, ts=t + 5)
    c.add("crypto15", "btc", "REJECT_DEPTH", None, ts=t + 6)
    c.add("crypto15", "btc", "OBSERVE", 10.0, ts=t + 3700)               # heure suivante
    d = c.drain()
    assert len(d) == 3 and c.data == {}
    obs_first = [v for k, v in d.items() if k[3] == "OBSERVE" and v[0] == 2][0]
    assert obs_first == [2, 40.0, -60.0]                                 # n, meilleur, somme
    assert asyncio.run(Counters().flush(None)) == 0                      # rien à écrire : pas d'accès base
