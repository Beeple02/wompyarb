# arbwatch — recherche d'arb Kalshi ↔ Polymarket US (15 min up/down) — PAPER ONLY

Collecte les order books des marchés 15 min BTC/ETH/SOL/XRP sur les deux venues, calcule l'écart
de prix, ouvre des **paper trades** simulés, puis mesure après résolution si les deux venues ont
résolu pareil. Aucun ordre n'est jamais envoyé (endpoints de lecture uniquement).

## Ce qu'il faut croire (et ne pas croire)

- **`gross` / `net` / `net_bps` ne sont PAS un profit espéré.** `gross = 1 − (a + b)` est l'écart de
  prix entre venues. Si chaque venue price correctement sa propre résolution, l'espérance de
  payout du trade vaut la somme des deux prix : zéro avant fees. L'écart n'est « gagné » que si les
  deux résolutions concordent ; il rémunère (ou pas) le risque de désaccord.
- **Le critère de décision est le P&L réalisé** des paper trades réglés, par tranche d'écart
  (`/api/stats/pnl`, `python -m arb.report`), pas `net_bps`.
- `p̂_dis` (taux de désaccord, `/api/stats/dis`) ne compte que les résolutions `official/official`.

| Sujet | Hypothèse | Statut |
|---|---|---|
| Kalshi résolution | moyenne 60 s de BRTI à t₁ vs à t₀, égalité → YES | vu sur BTC ; ETH/SOL/XRP non vérifiés |
| Polymarket résolution | Chainlink, point-in-time, `>=` → Up | vu sur l'international ; polymarket.us à lire |
| Endpoints Polymarket US | `/v1/events/slug/..`, `/v1/markets/{slug}/book` | confirmés en prod (200) |
| Fees Polymarket | placeholder `0.07·p(1-p)·N` | **à remplacer par la vraie grille** |

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
| `GET /api/stats/dis` · `/stats/pnl` · `/stats/signals?hours=` | p̂_dis + IC, P&L (par actif, par écart, payouts, equity curve), comptes de signaux |
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
