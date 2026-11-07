# arbwatch — recherche d'arb Kalshi ↔ Polymarket US (15 min up/down) — PAPER ONLY

Collecte les order books des marchés 15 min BTC/ETH/SOL/XRP sur les deux venues, calcule l'edge
net, ouvre des **paper trades** simulés, puis mesure après résolution si les deux venues ont
résolu pareil. La métrique centrale est `p̂_dis` (taux de désaccord), pas le nombre de signaux.

Aucun ordre n'est jamais envoyé : le code n'utilise que des endpoints de lecture.

## Ce que le code suppose (à vérifier)

| Sujet | Hypothèse | Statut |
|---|---|---|
| Kalshi résolution | moyenne 60 s de BRTI à t₁ vs moyenne 60 s à t₀, égalité → YES | vu dans les règles BTC ; ETH/SOL/XRP non vérifiés |
| Polymarket résolution | Chainlink data stream, point-in-time, `>=` → Up | vu sur polymarket **international** ; polymarket.us à lire à la main |
| Endpoints Kalshi | REST public `/markets`, `/markets/{t}/orderbook` | de mémoire, parser tolérant |
| Endpoints Polymarket US | chemins par défaut = **supposition** | lance `scripts/probe.py`, ajuste `PM_*_PATH` |
| Book Polymarket | le book public est celui de Up (long) | `PM_BOOK_IS_UP=false` si inversé |
| Fees Polymarket | placeholder `0.07·p(1-p)·N` | **remplace par la vraie grille** |

Conséquence : G1 (même indice) est `FALSE` par défaut dans la table `assets`. Le moteur utilise
donc un `p_dis` = prior (`PRIOR_P_DIS`, 10 %) mélangé à l'empirique (`PRIOR_STRENGTH` fenêtres).

## Modèle de risque (différent du spec initial)

Direction `PU_KD` = Up sur Polymarket + Down sur Kalshi. Accord des venues → payout 1.
Désaccord → payout 2 (PM up/KS down) ou 0 (PM down/KS up).

- Si les deux désaccords sont aussi fréquents, l'espérance reste `1 − C` ; seule la variance monte.
- Si un biais existe, une direction a une espérance > 1 et l'autre < 1. Le moteur estime
  `asym = P(PU_KD) − P(PD_KU)` (rétréci vers 0) et l'ajoute/retranche selon la direction.
- `DIS_HAIRCUT=1.0` rétablit la charge conservative du spec (`p_dis·C`) ; `LAMBDA` ajoute
  `λ·√p_dis`. Défauts à 0 : on mesure d'abord. Le rapport donne le Sharpe par trade.

## Lancer

```bash
pip install -r requirements-dev.txt
docker compose up -d db
export DATABASE_URL=postgresql://arb:arb@localhost:5432/arb
PYTHONPATH=src python scripts/probe.py btc     # 1) vérifier les endpoints
PYTHONPATH=src python -m arb.main              # 2) collecteur + resolver
PYTHONPATH=src python -m arb.report            # 3) p̂_dis, signaux, paper P&L
PYTHONPATH=src python -m pytest -q
```

## Railway

1. Push sur GitHub, puis New Project → Deploy from GitHub repo (le `Dockerfile` est détecté).
2. Ajoute le plugin **Postgres** ; Railway injecte `DATABASE_URL` (référence `${{Postgres.DATABASE_URL}}`
   si besoin) dans le service.
3. Variables : copie `.env.example`. Pas de port à exposer : c'est un worker.
4. Rapport : `railway run python -m arb.report` (avec `PYTHONPATH=src`) ou connexion directe à Postgres.

## Fonctionnement

- `main.collector_loop` : toutes les `POLL_INTERVAL_S` s, par actif, découvre la fenêtre courante
  (Kalshi via `min/max_close_ts`, Polymarket via le slug `{asset}-updown-15m-YYYY-MM-DD-HHMMz` en UTC),
  récupère les 2 books en parallèle, stocke les quotes, évalue, enregistre le signal, ouvre
  au plus 1 paper trade par fenêtre (VWAP à `PAPER_SIZE`, pas d'entrée dans les 5 premières
  ni 60 dernières secondes).
- `main.resolver_loop` : après `RESOLVE_DELAY_S`, lit le résultat officiel des deux venues,
  règle les paper trades, enregistre le type de désaccord. Si Polymarket ne renvoie rien après
  10 min, résultat `inferred` depuis le dernier prix ; ces lignes sont **exclues** de `p̂_dis`.
- Rétention : `quotes` et rejets purgés après `RETENTION_DAYS`.

## Limites connues

- REST polling (≈2 s) : pas de WebSocket (Polymarket US en exige avec clés, Kalshi aussi).
  Les timestamps sont ceux de réception, donc la fraîcheur mesure la latence de fetch, pas celle de l'exchange.
- Le paper fill suppose un taker au VWAP du snapshot, sans impact ni file d'attente.
- Pas de modèle de capital, bridging ou minimums d'exchange.
