from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from .engine import Evaluation, dis_kind, payout
from .models import Book

log = logging.getLogger("arb.db")

ASSET_SEED = {
    "btc": ("KXBTC15M", "CF Benchmarks BRTI (moyenne 60 s)"),
    "eth": ("KXETH15M", "CF Benchmarks ETH-RTI (moyenne 60 s) - NON VERIFIE"),
    "sol": ("KXSOL15M", "CF Benchmarks SOL-RTI (moyenne 60 s) - NON VERIFIE"),
    "xrp": ("KXXRP15M", "CF Benchmarks XRP-RTI (moyenne 60 s) - NON VERIFIE"),
}


class DatabaseError(RuntimeError):
    """Erreur de connexion DB avec un message actionnable (pas de traceback asyncpg)."""


class DatabaseConfigError(DatabaseError):
    """DATABASE_URL invalide : réessayer ne changera rien."""


class DatabaseUnavailableError(DatabaseError):
    """La base n'a pas répondu dans le budget imparti (DB éteinte, DNS, réseau...)."""


# Paramètres libpq qu'asyncpg ne connaît pas : laissés dans l'URL, il les envoie comme réglages
# serveur et Postgres répond "unrecognized configuration parameter" (URLs Supabase/Neon/pgBouncer).
_LIBPQ_ONLY = frozenset({
    "channel_binding", "pgbouncer", "options", "gssencmode", "requiressl", "sslcompression",
    "connect_timeout", "keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count",
    "tcp_user_timeout", "replication", "fallback_application_name", "service",
})
_SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
# Erreurs définitives : inutile de boucler, il faut corriger la config.
_FATAL_SQLSTATES = {
    "28P01": "mot de passe refusé (28P01)",
    "28000": "authentification refusée (28000)",
    "3D000": "la base n'existe pas (3D000)",
    "3F000": "le schéma n'existe pas (3F000)",
}
_RAILWAY_DNS_HINT = (
    "Le nom d'hôte ne résout pas (DNS). Sur Railway, vérifie dans cet ordre :\n"
    "  1. le service Postgres est déployé et RUNNING (après un wipe du volume, il réinitialise\n"
    "     le cluster au démarrage : le nom privé n'existe pas tant qu'il n'est pas prêt) ;\n"
    "  2. l'app et Postgres sont dans le MÊME projet ET le MÊME environnement (le DNS privé\n"
    "     *.railway.internal est isolé par projet/environnement, il ne résout jamais sinon) ;\n"
    "  3. la variable du service app est DATABASE_URL=${{Postgres.DATABASE_URL}} (et non 'db',\n"
    "     'localhost' ou une URL recopiée d'un autre projet) ;\n"
    "  4. secours : active le TCP proxy public du Postgres puis ajoute\n"
    "     DATABASE_PUBLIC_URL=${{Postgres.DATABASE_PUBLIC_URL}} -> utilisé automatiquement ici."
)


def clean_dsn(url: str) -> str:
    """Valide/normalise une DSN Postgres et retire les paramètres libpq-only.

    Lève DatabaseConfigError (message actionnable) si l'URL est inutilisable : vide, template
    Railway non résolu, mauvais schéma, hôte absent.
    """
    dsn = (url or "").strip().strip('"').strip("'")
    if not dsn:
        raise DatabaseConfigError(
            "DATABASE_URL est vide. Lie le plugin Postgres au service : "
            "DATABASE_URL=${{Postgres.DATABASE_URL}} (Railway) ou "
            "postgresql://arb:arb@localhost:5432/arb (local).")
    if "${{" in dsn or "}}" in dsn:
        raise DatabaseConfigError(
            f"DATABASE_URL contient un template non résolu ({dsn!r}). Railway ne remplace "
            "${{Postgres.X}} que si le service Postgres existe dans le même projet/environnement : "
            "vérifie le nom du service référencé, ou colle l'URL PostgreSQL complète.")
    if "://" not in dsn:
        raise DatabaseConfigError(
            f"DATABASE_URL n'est pas une URL ({dsn!r}). Format attendu : "
            "postgresql://user:motdepasse@hote:5432/base (le format libpq 'host=... port=...' "
            "n'est pas accepté par asyncpg).")
    try:
        parts = urlsplit(dsn)
        host, port = parts.hostname, parts.port
    except ValueError as e:
        raise DatabaseConfigError(f"DATABASE_URL illisible ({dsn!r}) : {e}") from e
    if parts.scheme.lower() not in ("postgres", "postgresql"):
        raise DatabaseConfigError(
            f"Schéma {parts.scheme!r} invalide dans DATABASE_URL : utilise postgresql:// "
            "(mysql:// ou sqlite:// ne sont pas supportés).")
    if not host:
        raise DatabaseConfigError(f"Aucun hôte dans DATABASE_URL ({dsn!r}) : il manque @hote:port/base.")
    kept, dropped = [], []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        (dropped if k.lower() in _LIBPQ_ONLY else kept).append((k, v))
    if dropped:
        log.info("DATABASE_URL : paramètres libpq ignorés %s", ", ".join(k for k, _ in dropped))
    return urlunsplit(("postgresql", parts.netloc, parts.path, urlencode(kept), ""))


def describe(url: str) -> str:
    """Cible de connexion journalisable : user@hote:port/base, jamais le mot de passe."""
    try:
        p = urlsplit(clean_dsn(url))
        return f"{p.username or 'postgres'}@{p.hostname}:{p.port or 5432}{p.path or '/postgres'}"
    except (DatabaseError, ValueError):
        return "<DATABASE_URL invalide>"


def _ssl_arg(ssl: str | None):
    if ssl is None or not str(ssl).strip():
        return None                      # défaut asyncpg : 'prefer' (TLS si dispo, sinon clair)
    v = str(ssl).strip().lower()
    if v not in _SSL_MODES:
        raise DatabaseConfigError(f"DB_SSL={ssl!r} invalide : {', '.join(_SSL_MODES)}")
    return v


async def resolve_host(host: str, port: int, timeout: float = 5.0) -> list[str]:
    """Pré-vol DNS : distingue 'nom qui ne résout pas' de 'port fermé' avant de créer le pool."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout)
    return sorted({i[4][0] for i in infos})


def _fatal_message(e: BaseException, target: str) -> str | None:
    """Message actionnable si l'erreur est définitive, None si elle est transitoire."""
    reason = _FATAL_SQLSTATES.get(getattr(e, "sqlstate", None) or "")
    if reason:
        return (f"{reason} sur {target}. Vérifie utilisateur/mot de passe/nom de base de "
                "DATABASE_URL (Railway : recopie ${{Postgres.DATABASE_URL}} plutôt qu'une URL "
                "d'un autre projet).")
    if isinstance(e, asyncpg.exceptions.ClientConfigurationError):
        return f"Configuration de connexion refusée par asyncpg sur {target} : {e}"
    return None


def _is_transient(e: BaseException) -> bool:
    # OSError couvre gaierror (DNS), ConnectionRefused/Reset, timeouts socket et ssl.SSLError.
    return isinstance(e, (OSError, asyncio.TimeoutError, asyncpg.PostgresError))


def _backoff(attempt: int, retry_max_s: float) -> float:
    """1 s, 2 s, 4 s, 8 s ... plafonné à retry_max_s, avec ±20 % de jitter."""
    return max(0.05, min(retry_max_s, 1.0 * 2 ** min(attempt - 1, 6)) * (0.8 + 0.4 * random.random()))


async def _try_once(dsn: str, ssl, timeout: float) -> asyncpg.Pool:
    parts = urlsplit(dsn)
    await resolve_host(parts.hostname, parts.port or 5432)
    pool = None
    ok = False
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, timeout=timeout, ssl=ssl)
        async with pool.acquire() as c:      # pool créé != base utilisable : on valide
            await c.fetchval("SELECT 1")
        ok = True
        return pool
    finally:
        if pool is not None and not ok:
            pool.terminate()                 # ne laisse pas de connexions orphelines


async def _connect_one(dsn: str, wait_s: float, retry_max_s: float, ssl, timeout: float) -> asyncpg.Pool:
    target = describe(dsn)
    parts = urlsplit(dsn)
    deadline = time.monotonic() + max(0.0, wait_s)
    attempt = 0
    last: BaseException | None = None
    while True:
        attempt += 1
        try:
            pool = await _try_once(dsn, ssl, timeout)
            log.info("DB connectée : %s (tentative %d)", target, attempt)
            return pool
        except Exception as e:
            fatal = _fatal_message(e, target)
            if fatal:
                raise DatabaseConfigError(fatal) from e
            if not _is_transient(e):
                raise                        # vrai bug : on ne le masque pas
            last = e
            dns = isinstance(e, socket.gaierror)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = (f"Base injoignable après {wait_s:.0f} s / {attempt} tentative(s) : {target}\n"
                       f"Dernière erreur : {type(e).__name__}: {e}")
                if dns:
                    msg += "\n" + _RAILWAY_DNS_HINT
                elif isinstance(e, ConnectionRefusedError):
                    msg += ("\nLe nom résout mais le port ne répond pas : Postgres démarre encore, "
                            "ou le port de DATABASE_URL n'est pas 5432 (TCP proxy public = port différent).")
                elif isinstance(e, asyncio.TimeoutError):
                    msg += ("\nDélai dépassé (DNS ou TCP) : hôte injoignable ou réseau très lent. "
                            "Vérifie l'hôte/port de DATABASE_URL et que le service Postgres est bien up.")
                raise DatabaseUnavailableError(msg) from last
            if attempt == 1:
                log.warning("DB pas encore prête (%s) : %s: %s%s", target, type(e).__name__, e,
                            "\n" + _RAILWAY_DNS_HINT if dns else "")
            elif attempt % 5 == 0:
                log.warning("DB toujours injoignable (%s, tentative %d, %.0f s restantes) : %s",
                            target, attempt, remaining, e)
            await asyncio.sleep(min(_backoff(attempt, retry_max_s), remaining))


async def connect(url: str, *, fallback_url: str = "", wait_s: float = 300.0,
                  retry_max_s: float = 15.0, ssl: str | None = None,
                  timeout: float = 10.0) -> asyncpg.Pool:
    """Pool Postgres robuste au démarrage (Railway/Render/Docker).

    - valide l'URL et retire les paramètres libpq-only ;
    - attend la base avec backoff exponentiel (DNS pas encore propagé, Postgres qui réinitialise
      un volume, connexion refusée) au lieu de tuer le process au premier gaierror ;
    - bascule sur `fallback_url` (DATABASE_PUBLIC_URL) si l'hôte privé ne résout pas ;
    - échoue vite et clairement sur une erreur définitive (mot de passe, base inexistante).

    wait_s <= 0 = une seule tentative (CLI). Lève DatabaseError avec un message actionnable.
    """
    ssl = _ssl_arg(ssl)
    candidates: list[tuple[str, str]] = []
    errors: list[str] = []
    try:
        candidates.append((clean_dsn(url), "primaire"))
    except DatabaseConfigError as e:
        if not fallback_url.strip():
            raise
        errors.append(f"[primaire] {e}")
        log.error("DATABASE_URL inutilisable : %s", e)
    if fallback_url.strip():
        try:
            fb = clean_dsn(fallback_url)
        except DatabaseConfigError as e:
            log.warning("URL de secours ignorée : %s", e)
        else:
            if not candidates or fb != candidates[0][0]:
                candidates.append((fb, "secours public"))
    if not candidates:
        raise DatabaseConfigError("Aucune URL Postgres utilisable (DATABASE_URL / DATABASE_PUBLIC_URL). "
                                  + " ".join(errors))
    start = time.monotonic()
    for i, (dsn, label) in enumerate(candidates):
        left = max(0.0, wait_s - (time.monotonic() - start))
        # avec un secours configuré, on ne s'acharne pas 5 min sur le DNS privé
        budget = left if i == len(candidates) - 1 else min(left, 60.0)
        log.info("connexion DB (%s) : %s", label, describe(dsn))
        try:
            return await _connect_one(dsn, budget, retry_max_s, ssl, timeout)
        except DatabaseConfigError:
            raise                            # erreur définitive : inutile d'essayer ailleurs
        except DatabaseError as e:
            errors.append(f"[{label}] {e}")
            if i < len(candidates) - 1:
                log.warning("Échec %s, bascule sur l'URL de secours.", label)
    raise DatabaseUnavailableError("\n\n".join(errors))


async def init(pool: asyncpg.Pool) -> None:
    sql = (Path(__file__).parent / "schema.sql").read_text()
    async with pool.acquire() as c:
        await c.execute(sql)
        for sym, (series, ks_idx) in ASSET_SEED.items():
            await c.execute(
                """INSERT INTO assets(symbol, ks_series, pm_prefix, ks_index, pm_index,
                       ks_tie_rule, pm_tie_rule, ks_void_rule, pm_void_rule,
                       g1_same_index, g2_same_tie_void, rules_verified_on, rules_verified_by, notes)
                   VALUES($1,$2,$3,$4,$5,'YES si egal','Up si egal (version intl)','?','?',
                          FALSE, NULL, DATE '2026-11-07', 'recherche web (BTC uniquement)',
                          'A verifier a la main sur polymarket.us')
                   ON CONFLICT (symbol) DO NOTHING""",
                sym, series, f"{sym}-updown-15m", ks_idx,
                "Chainlink Data Streams (point-in-time) - version intl, a verifier sur polymarket.us")


async def get_or_create_window(pool, asset: str, start: datetime, end: datetime,
                               ks_ticker: str, pm_slug: str) -> int:
    async with pool.acquire() as c:
        return await c.fetchval(
            """INSERT INTO windows(asset,t_start,t_end,ks_ticker,pm_slug) VALUES($1,$2,$3,$4,$5)
               ON CONFLICT (asset,t_start) DO UPDATE SET ks_ticker=EXCLUDED.ks_ticker,
                   pm_slug=EXCLUDED.pm_slug RETURNING id""",
            asset, start, end, ks_ticker, pm_slug)


async def insert_quote(pool, window_id: int, book: Book, with_ladders: bool = False) -> None:
    ladders = json.dumps({"up": book.asks_up[:5], "down": book.asks_down[:5]}) if with_ladders else None
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO quotes(window_id,venue,ts_recv,best_ask_up,best_ask_down,depth_up,depth_down,ladders)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)""",
            window_id, book.venue, datetime.fromtimestamp(book.ts_recv, tz=timezone.utc),
            book.best("up"), book.best("down"), book.depth("up"), book.depth("down"), ladders)


async def insert_signal(pool, window_id: int, ev: Evaluation, secs_to_close: float) -> None:
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO signals(window_id,ts,secs_to_close,status,reason,direction,n,vwap_pm,vwap_ks,
                   fees_total,cost_per,gross,net,net_bps,p_dis,slippage)
               VALUES($1,now(),$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
            window_id, secs_to_close, ev.status, ev.reason, ev.direction, ev.n, ev.vwap_pm, ev.vwap_ks,
            ev.fees_total, ev.cost_per, ev.gross, ev.net, ev.net_bps, ev.p_dis, ev.slippage)


async def open_paper_trade(pool, window_id: int, ev: Evaluation) -> bool:
    async with pool.acquire() as c:
        r = await c.fetchval(
            """INSERT INTO paper_trades(window_id,ts,direction,n,vwap_pm,vwap_ks,fees_total,expected_net)
               VALUES($1,now(),$2,$3,$4,$5,$6,$7) ON CONFLICT (window_id) DO NOTHING RETURNING id""",
            window_id, ev.direction, ev.n, ev.vwap_pm, ev.vwap_ks, ev.fees_total, ev.net * ev.n)
        return r is not None


async def has_trade(pool, window_id: int) -> bool:
    async with pool.acquire() as c:
        return bool(await c.fetchval("SELECT 1 FROM paper_trades WHERE window_id=$1", window_id))


async def pending_windows(pool, delay_s: float, max_age_s: float = 3 * 3600):
    now = datetime.now(timezone.utc)
    async with pool.acquire() as c:
        return await c.fetch(
            """SELECT w.* FROM windows w LEFT JOIN resolutions r ON r.window_id=w.id
               WHERE w.t_end < $1 AND w.t_end > $2 AND COALESCE(r.complete, FALSE) = FALSE
               ORDER BY w.t_end""",
            now - timedelta(seconds=delay_s), now - timedelta(seconds=max_age_s))


async def last_pm_mid(pool, window_id: int, t_end: datetime) -> float | None:
    """P(Up) approx. du dernier quote Polymarket, seulement s'il date de < 20 s avant la clôture."""
    async with pool.acquire() as c:
        r = await c.fetchrow(
            """SELECT best_ask_up, best_ask_down, ts_recv FROM quotes
               WHERE window_id=$1 AND venue='polymarket' AND ts_recv <= $2
               ORDER BY ts_recv DESC LIMIT 1""", window_id, t_end)
    if not r or r["best_ask_up"] is None or r["best_ask_down"] is None:
        return None
    if (t_end - r["ts_recv"]).total_seconds() > 20:
        return None
    return (r["best_ask_up"] + (1 - r["best_ask_down"])) / 2


async def save_resolution(pool, window_id: int, ks: str | None, pm: str | None,
                          ks_src: str | None, pm_src: str | None,
                          ks_strike=None, ks_settle=None, pm_strike=None, pm_settle=None) -> bool:
    complete = ks is not None and pm is not None
    kind = dis_kind(pm, ks) if complete else None
    async with pool.acquire() as c:
        async with c.transaction():
            await c.execute(
                """INSERT INTO resolutions(window_id,ks_result,pm_result,ks_source,pm_source,dis_kind,complete,
                       ks_strike,ks_settle,pm_strike,pm_settle)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                   ON CONFLICT (window_id) DO UPDATE SET ks_result=EXCLUDED.ks_result,
                     pm_result=EXCLUDED.pm_result, ks_source=EXCLUDED.ks_source,
                     pm_source=EXCLUDED.pm_source, dis_kind=EXCLUDED.dis_kind,
                     complete=EXCLUDED.complete,
                     ks_strike=COALESCE(EXCLUDED.ks_strike, resolutions.ks_strike),
                     ks_settle=COALESCE(EXCLUDED.ks_settle, resolutions.ks_settle),
                     pm_strike=COALESCE(EXCLUDED.pm_strike, resolutions.pm_strike),
                     pm_settle=COALESCE(EXCLUDED.pm_settle, resolutions.pm_settle),
                     updated_at=now()""",
                window_id, ks, pm, ks_src, pm_src, kind, complete,
                ks_strike, ks_settle, pm_strike, pm_settle)
            if complete:
                for t in await c.fetch(
                        "SELECT id,direction,n,vwap_pm,vwap_ks,fees_total FROM paper_trades "
                        "WHERE window_id=$1 AND status='open'", window_id):
                    pay = payout(t["direction"], pm, ks) * t["n"]
                    pnl = pay - t["n"] * (t["vwap_pm"] + t["vwap_ks"]) - t["fees_total"]
                    await c.execute(
                        "UPDATE paper_trades SET status='settled',payout=$2,pnl=$3,settled_at=now() WHERE id=$1",
                        t["id"], pay, pnl)
    return complete


async def dis_counts(pool) -> dict[str, tuple[int, int, int]]:
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT w.asset, count(*) n,
                      count(*) FILTER (WHERE r.dis_kind='PU_KD') k1,
                      count(*) FILTER (WHERE r.dis_kind='PD_KU') k2
               FROM resolutions r JOIN windows w ON w.id=r.window_id
               WHERE r.complete AND r.ks_source='official' AND r.pm_source='official'
               GROUP BY w.asset""")
    return {r["asset"]: (r["n"], r["k1"], r["k2"]) for r in rows}


async def prune(pool, days: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with pool.acquire() as c:
        await c.execute("DELETE FROM quotes WHERE ts_recv < $1", cutoff)
        await c.execute("DELETE FROM daily_signals WHERE ts < $1", cutoff)
        await c.execute("DELETE FROM weather_signals WHERE ts < $1", cutoff)
        await c.execute("DELETE FROM signals WHERE ts < $1 AND status IN ('REJECT_STALE','REJECT_DEPTH')", cutoff)


async def audit(pool, actor: str, event: str, payload: dict) -> None:
    async with pool.acquire() as c:
        await c.execute("INSERT INTO audit_log(actor,event,payload) VALUES($1,$2,$3::jsonb)",
                        actor, event, json.dumps(payload))
