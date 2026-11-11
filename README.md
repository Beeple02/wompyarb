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

## Marchés journaliers above/below (0.4.0)

Kalshi (`KXBTCD`, `KXETHD`, `KXSOLD`, `KXXRPD`) et Polymarket US (`{asset}-above-day-AAAA-MM-JJ`) listent des
échelles de seuils « $X or above » réglées sur la moyenne de 60 prix de l'indice CF Benchmarks dans la dernière
minute avant 17:00 ET (22:00Z en hiver). Mêmes règles, mêmes heures : **un seuil identique = le même événement**.
Yes d'un côté + No de l'autre paie exactement 1 si les deux résolvent pareil.

Boucle (`daily.py`) :
1. **Scan** toutes les 10 min : marchés Kalshi ouverts (groupés par événement, heure déduite du ticker
   `KXBTCD-26NOV0817` = 8 nov 17:00 New York) + événements Polymarket des 3 prochains jours ; ne garde que les
   couples dont l'heure de règlement concorde (±120 s) et dont le seuil est identique.
2. **Quotes** toutes les 20 s, seulement pour les seuils proches de la monnaie (prix Kalshi entre 0.03 et 0.97) :
   lit les deux books, évalue (même calcul de VWAP et de fees que les 15 min), ouvre au plus 1 paper trade par seuil.
   Pas d'entrée dans les 10 dernières minutes avant le règlement.
3. **Résolution** après 17:00 ET : lit les résultats officiels des deux venues, règle les trades.
Quand l'horloge passe au jour suivant, le scan suivant trouve les nouveaux événements : la boucle repart seule.

Points de vigilance : arrondi des deux venues (Kalshi « above 73499.99 » contre Polymarket « moyenne arrondie à
2 décimales ≥ 73 500 ») : écart possible sur une fenêtre de 0,01 $, mesuré par `daily_resolutions.dis_kind` ;
capital immobilisé jusqu'à 24 h par trade (le ROC se lit par jour, pas par 15 min) ; formats JSON des événements
journaliers non encore observés : `scripts/probe_daily.py` affiche le scan sans base de données.

Routes : `/api/daily/overview`, `/api/daily/pairs`, `/api/daily/signals`, `/api/daily/trades`, `/api/daily/stats`.

## Marchés météo : pluie par ville (0.5.0)

Kalshi `KXRAIN` (« Where will it rain today ? ») <-> Polymarket **international** (polymarket.com : API publique Gamma +
CLOB, distincte de polymarket.us). Kalshi n'a pas de plateforme « internationale » séparée : `api.elections.kalshi.com`
sert tout le monde, le côté Kalshi est inchangé. Depuis 0.7.0, le moteur crypto 15 min utilise lui aussi Polymarket international (voir ci-dessous).

Le scanner n'autorise un trade qu'après avoir vérifié l'équivalence des deux contrats :
- **ville + station NWS** (codes `CLIxxx`, `issuedby=`, ICAO lus dans les règles) : `match` / `mismatch` / `assumed`.
  `assumed` (une règle sans code) n'est négociable que pour les villes de `WEATHER_TRUSTED_CITIES` (défaut `auto` : les villes à station unique ; les villes ambiguës CHI, NYC, DFW, HOU, LAX, SFO, DCA exigent un code des deux côtés) ;
- **jour climatologique** (minuit à minuit, heure locale standard) ;
- **trace de pluie « T »** : lue dans les règles des deux côtés. Si elles diffèrent, seule la direction qui paie
  toujours >= 1 est évaluée (Yes là où la trace compte + No sur l'autre : paie 2 les jours de trace, 1 sinon) ;
- règle illisible ou stations différentes : le couple est listé avec la raison, jamais tradé.

Cycle : scan toutes les 10 min -> books (Kalshi + CLOB) toutes les 30 s -> paper trade au plus 1 par ville et par jour
-> résolution à partir d'1 h après la fin du jour climatologique (backoff) -> rescan. Priorité au jour en cours
(seuil d'écart 100 bps aujourd'hui, 300 bps pour demain). Les books vides sont normaux (marchés sans trade) :
seuls les couples avec une profondeur suffisante des deux côtés produisent une évaluation.

Routes : `/api/weather/overview`, `/candidates` (toutes les villes communes avec verdict et règles), `/signals`,
`/trades`, `/stats`. `scripts/probe_weather.py` affiche le scan sans base de données.

## Stockage : tenir dans ~500 Mo (0.6.0)

Mesure sur 23 min de collecte avant correctif : ≈ 290 000 cotes/jour (≈ 120 Mo) + ≈ 146 000 signaux/jour (≈ 40 Mo),
soit ~150 Mo/jour pour le seul 15 min, bien plus avec les marchés journaliers et météo. Désormais :
- cotes brutes **échantillonnées** (1 toutes les 10 s, sans le détail des 5 niveaux), gardées 24 h ;
- signaux : `REJECT_*` jamais stockés (comptés dans `signal_counts`, 1 ligne par heure/actif/statut), `OBSERVE` 1 toutes les
  30 s (120 s pour journalier/météo) sauf quasi-opportunités, `ACTIONABLE`/`UNVERIFIED` toujours ; gardés 3 jours ;
- trades, résolutions, paires, fenêtres et audit : jamais purgés (quelques Mo) ;
- garde-fou toutes les 5 min : 70 % du plafond -> purge anticipée ; 90 % -> `TRUNCATE` des cotes et signaux (libère le
  disque immédiatement). Taille et tables les plus lourdes dans `/api/status` (`db`).
- `/api/live` lit la dernière évaluation en mémoire ; `/api/stats/signals?scope=crypto15|daily|weather` lit les compteurs
  agrégés : ils restent exacts malgré l'échantillonnage.
Estimation en régime établi : < 100 Mo.

## Polymarket international partout (0.7.0)

`PM_FLAVOR=intl` (défaut) : le moteur 15 min lit Polymarket **international** (Gamma + CLOB, sans clé). Identifiant du marché :
`{asset}-updown-15m-{epoch du début de fenêtre}` (ex. `btc-updown-15m-1776453300` = 17 avr 15h15 ET). Le côté « Up » est
repéré par son nom (`outcomes`), plus par une supposition de sens du carnet. `PM_FLAVOR=us` rétablit l'ancien client.

Conséquences à connaître (sources lues, pas encore confrontées à une réponse réelle) :
- **Résolution différente de Kalshi** : Polymarket intl règle les 15 min sur un flux Chainlink (point dans le temps),
  Kalshi sur la moyenne BRTI de 60 s. Un désaccord de résolution redevient possible : `p_dis` (prior 10 %) et
  `resolutions.dis_kind` le mesurent. L'écart de prix n'est un profit que si les deux venues résolvent pareil.
- **Frais taker dynamiques** sur Polymarket intl (formule publiée `C·p·feeRate·(p·(1−p))^exponent`, exposant retiré
  ensuite pour certaines catégories ; paramètres par type de marché, endpoint `/fee-rate?token_id=`). `PM_INTL_FEE_COEF`
  est un **placeholder** ; `scripts/probe_crypto_intl.py` affiche la réponse de `/fee-rate`.
- **Marchés journaliers crypto désactivés** (`DAILY_ENABLED=false`) : les « Bitcoin above ___ on <date> » de Polymarket intl se
  règlent sur la bougie Binance BTC/USDT de **12:00 ET**, alors que Kalshi règle à **17:00 ET** sur la moyenne BRTI : ce n'est
  pas le même événement, le scanner n'aurait aucun couple valide.
- `/api/stats/basis` est vide (pas de valeur de référence côté Polymarket intl).

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
| `GET /api/daily/overview` · `/daily/pairs` · `/daily/signals` · `/daily/trades` · `/daily/stats` | marchés journaliers (voir section dédiée) |
| `GET /api/weather/overview` · `/weather/candidates` · `/weather/signals` · `/weather/trades` · `/weather/stats` | pluie par ville |
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
