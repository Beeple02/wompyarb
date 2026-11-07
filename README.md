# arbwatch — recherche d'arb Kalshi ↔ Polymarket US (15 min up/down) — PAPER ONLY

Collecte les order books des marchés 15 min BTC/ETH/SOL/XRP sur les deux venues, calcule l'écart
de prix, ouvre des **paper trades** simulés, puis mesure après résolution si les deux venues ont
résolu pareil. Aucun ordre n'est jamais envoyé (endpoints de lecture uniquement).

## Ce qu'il faut croire (et ne pas croire)

- **`gross` / `net` / `net_bps` ne sont un profit que si les deux venues résolvent pareil** (ce que 0.3.0 permet de vérifier).
  Avant cette vérification, ne les lis pas comme un profit espéré.
- Détail historique : `gross = 1 − (a + b)` est l'écart de
  prix entre venues. Si chaque venue price correctement sa propre résolution, l'espérance de
  payout du trade vaut la somme des deux prix : zéro avant fees. L'écart n'est « gagné » que si les
  deux résolutions concordent ; il rémunère (ou pas) le risque de désaccord.
- **Le critère de décision est le P&L réalisé** des paper trades réglés, par tranche d'écart
  (`/api/stats/pnl`, `python -m arb.report`), pas `net_bps`.
- `p̂_dis` (taux de désaccord, `/api/stats/dis`) ne compte que les résolutions `official/official`.

| Sujet | Constat | Statut |
|---|---|---|
| Kalshi résolution | moyenne de 60 s de l'indice CF Benchmarks (BRTI, ETHUSD_RTI...) à t₁ vs t₀, égalité → YES | vu sur BTC ; autres actifs à vérifier |
| Polymarket US résolution | **même indice et même méthode** : 60 prix de l'indice RTI dans la dernière minute, moyenne arrondie à 2 décimales, `>=` → Up | **lu dans la règle du marché** (BTC et ETH) ; SOL/XRP à vérifier |
| Côté Up/Down Polymarket | `Yes` = côté long = Up (`marketSides[].long`) ; le book public est supposé être celui du côté long | cohérent, à confirmer par la répartition des payouts |
| Endpoint résultat Polymarket | `/v1/markets/slug/{slug}` renvoie 404 ; **`/v1/events/slug/{event}` porte le résultat** (`status`, `outcomePrices`, `priceToBeat`, `settlementPrice`) | utilisé par le résolveur depuis 0.3.0 |
| Fees Polymarket | `feeCoefficient` = 0.095 lu dans l'API ; formule `coef·p·(1−p)·N` supposée | formule à confirmer |

**Conséquence** : l'hypothèse « les deux venues résolvent différemment (Chainlink vs BRTI) » du premier
spec ne tient plus pour BTC/ETH sur polymarket.us. `p_dis` est probablement proche de 0 ; la table
`resolutions` stocke maintenant `pm_strike`, `pm_settle`, `ks_strike`, `ks_settle` pour le mesurer
directement (`/api/stats/basis`).

## Version du moteur, protection et wipe de la DB

- `src/arb/version.py` : `ENGINE_VERSION`. On l'incrémente à chaque patch moteur.
- Au démarrage : version différente de celle stockée dans `meta.engine_version` **et** DB non
  protégée → `TRUNCATE` de `windows, quotes, signals, paper_trades, resolutions` (on garde
  `assets`, `meta`, `audit_log`). Une DB sans `meta` (premier déploiement de cette version) est wipée.
- DB **protégée** (`meta.db_protected = 'true'`) : jamais wipée (ni au démarrage, ni via l'API).
  Si la version change quand même, les données sont gardées, un warning + une ligne `audit_log`
  signalent le mélange de versions.
- Marqueur : `POST /api/admin/protect` (poser), `POST /api/admin/unprotect` (`{"confirm":"UNPROTECT"}`).
- Wipe manuel : `POST /api/admin/wipe` avec `{"confirm":"WIPE"}` ; refusé (409) si protégée.

## API

Auth : en-têtes `X-API-Id` et `X-API-Key` (= variables `API_ID` / `API_KEY`). Sans ces variables
(clé ≥ 16 car.), l'API ne démarre pas. `GET /api/health` est public (ne renvoie ni données ni config).

| Route | Contenu |
|---|---|
| `GET /api/status` | version, protection, `data_since`, compteurs, fraîcheur des quotes, trades ouverts |
| `GET /api/config` | config courante (sans secrets) |
| `GET /api/live` | dernier signal + dernières cotes par actif |
| `GET /api/windows?asset=&limit=` | fenêtres + résolution |
| `GET /api/quotes?asset=&venue=&minutes=&limit=&ladders=1` | séries de cotes |
| `GET /api/signals?asset=&status=&minutes=&limit=` | signaux |
| `GET /api/trades?asset=&status=&limit=` | paper trades (écart, P&L/contrat, résolution) |
| `GET /api/resolutions?asset=&limit=` | résolutions |
| `GET /api/stats/dis` · `/stats/pnl` · `/stats/signals?hours=` · `/stats/basis?limit=` | p̂_dis + IC, P&L (par actif, par écart, payouts, equity curve), comptes de signaux, écart des valeurs de référence entre venues |
| `GET /api/logs?level=&contains=&after_id=&limit=` | logs en mémoire (3000 dernières lignes) |
| `GET /api/audit?limit=` | journal d'audit |
| `POST /api/admin/{protect,unprotect,wipe}` | voir ci-dessus |

Exemple : `curl -H "X-API-Id: $API_ID" -H "X-API-Key: $API_KEY" https://<domaine>.up.railway.app/api/status`

## Lancer en local

```bash
pip install -r requirements-dev.txt
docker compose up -d db
export DATABASE_URL=postgresql://arb:arb@localhost:5432/arb API_ID=local API_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
PYTHONPATH=src python -m arb.main
PYTHONPATH=src python -m arb.report
PYTHONPATH=src python -m pytest -q
```

## Railway

1. Service lié au repo GitHub (Dockerfile détecté) + plugin Postgres (`DATABASE_URL=${{Postgres.DATABASE_URL}}`).
2. Variables du service : `API_ID`, `API_KEY` (clé longue et aléatoire). Ne jamais les commiter.
3. Settings → Networking → **Generate Domain** pour joindre l'API depuis ton ordinateur (HTTPS).
   Railway fournit `PORT`.
4. Le dashboard local lit `API_ID` / `API_KEY` / l'URL dans ses propres variables d'environnement.

## Limites connues

- REST polling (~2 s), timestamps de réception : la fraîcheur mesure la latence de fetch.
- Paper fill = taker au VWAP du snapshot, sans impact ni file d'attente.
- Pendant un wipe via l'API, un tick en cours peut échouer une fois (clé étrangère) ; sans conséquence.
- Un ancien conteneur encore actif pendant un déploiement peut écrire quelques lignes après un
  wipe au démarrage ; en cas de doute, refais un wipe manuel juste après le déploiement.
