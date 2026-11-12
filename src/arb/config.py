from __future__ import annotations

import os
from dataclasses import dataclass, field


def _b(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    database_url: str = ""
    database_url_fallback: str = ""   # DATABASE_PUBLIC_URL : secours si le DNS privé ne résout pas
    db_wait_s: float = 300            # budget d'attente de la base au démarrage (0 = un seul essai)
    db_retry_max_s: float = 15        # plafond du backoff entre deux tentatives
    db_ssl: str = ""                  # vide = défaut asyncpg (prefer) ; disable|prefer|require|verify-full
    assets: tuple[str, ...] = ("btc", "eth", "sol", "xrp")

    paper_size: float = 10
    min_edge_bps: float = 20
    poll_interval_s: float = 2
    max_stale_s: float = 2
    max_skew_s: float = 1.5
    no_entry_first_s: float = 5
    no_entry_last_s: float = 60

    prior_p_dis: float = 0.10
    prior_strength: float = 50
    dis_haircut: float = 0.0
    lam: float = 0.0

    ks_fee_coef: float = 0.07
    pm_fee_coef: float = 0.095   # feeCoefficient lu dans l'API Polymarket US (formule supposée: coef*p*(1-p)*N)

    pm_flavor: str = "intl"          # "intl" (polymarket.com : Gamma + CLOB) ou "us" (gateway.polymarket.us)
    pm_intl_fee_coef: float = 0.07   # PLACEHOLDER : frais taker dynamiques, formule coef*p*(1-p)*N ; à vérifier

    kalshi_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    pm_base: str = "https://gateway.polymarket.us"
    pm_event_path: str = "/v1/events/slug/{slug}"
    pm_market_path: str = "/v1/markets/slug/{slug}"
    pm_book_path: str = "/v1/markets/{slug}/book"
    pm_book_is_up: bool = True

    retention_days: int = 7
    resolve_delay_s: float = 75
    log_level: str = "INFO"

    daily_enabled: bool = True    # scanner sur Polymarket US (client dédié, indépendant de PM_FLAVOR) : mêmes règles BRTI 17h ET que Kalshi
    daily_size: float = 10
    daily_poll_s: float = 20
    daily_scan_interval_s: float = 600
    daily_min_edge_bps: float = 50
    daily_no_entry_last_s: float = 600
    daily_band_lo: float = 0.03
    daily_band_hi: float = 0.97
    daily_concurrency: int = 6
    daily_days_ahead: int = 3
    daily_settle_tol_s: float = 120
    daily_resolve_delay_s: float = 120

    weather_enabled: bool = True
    weather_size: float = 10
    weather_poll_s: float = 30
    weather_scan_interval_s: float = 600
    weather_min_edge_bps: float = 100
    weather_min_edge_bps_next: float = 300
    weather_days_ahead: int = 1
    weather_max_stale_s: float = 10
    weather_trusted_cities: str = "auto"   # auto = villes à station unique ; all ; ou liste ; vide = aucune
    weather_resolve_delay_s: float = 3600
    pm_gamma_base: str = "https://gamma-api.polymarket.com"
    pm_clob_base: str = "https://clob.polymarket.com"

    quote_store_every_s: float = 10
    quote_store_ladders: bool = False
    signal_observe_every_s: float = 30
    daily_observe_every_s: float = 120
    weather_observe_every_s: float = 120
    quotes_retention_hours: float = 24
    signals_retention_days: float = 3
    db_max_mb: float = 500

    api_id: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    port: int = 8080
    api_cors: str = "*"


def _pm_fee_eff(self) -> float:
    return self.pm_intl_fee_coef if self.pm_flavor == "intl" else self.pm_fee_coef


Config.pm_fee_eff = property(_pm_fee_eff)   # coefficient de frais Polymarket selon la version utilisée


def load() -> Config:
    g = os.getenv
    d = Config()
    return Config(
        database_url=g("DATABASE_URL", "").strip(),
        database_url_fallback=(g("DATABASE_PUBLIC_URL", "") or g("DB_URL_FALLBACK", "")).strip(),
        db_wait_s=float(g("DB_WAIT_S", d.db_wait_s)),
        db_retry_max_s=float(g("DB_RETRY_MAX_S", d.db_retry_max_s)),
        db_ssl=g("DB_SSL", d.db_ssl).strip().lower(),
        assets=tuple(a.strip().lower() for a in g("ASSETS", "btc,eth,sol,xrp").split(",") if a.strip()),
        paper_size=float(g("PAPER_SIZE", d.paper_size)),
        min_edge_bps=float(g("MIN_EDGE_BPS", d.min_edge_bps)),
        poll_interval_s=float(g("POLL_INTERVAL_S", d.poll_interval_s)),
        max_stale_s=float(g("MAX_STALE_S", d.max_stale_s)),
        max_skew_s=float(g("MAX_SKEW_S", d.max_skew_s)),
        no_entry_first_s=float(g("NO_ENTRY_FIRST_S", d.no_entry_first_s)),
        no_entry_last_s=float(g("NO_ENTRY_LAST_S", d.no_entry_last_s)),
        prior_p_dis=float(g("PRIOR_P_DIS", d.prior_p_dis)),
        prior_strength=float(g("PRIOR_STRENGTH", d.prior_strength)),
        dis_haircut=float(g("DIS_HAIRCUT", d.dis_haircut)),
        lam=float(g("LAMBDA", d.lam)),
        ks_fee_coef=float(g("KS_FEE_COEF", d.ks_fee_coef)),
        pm_fee_coef=float(g("PM_FEE_COEF", d.pm_fee_coef)),
        pm_flavor=g("PM_FLAVOR", d.pm_flavor).strip().lower(),
        pm_intl_fee_coef=float(g("PM_INTL_FEE_COEF", d.pm_intl_fee_coef)),
        kalshi_base=g("KALSHI_BASE", d.kalshi_base).rstrip("/"),
        pm_base=g("PM_GATEWAY_BASE", d.pm_base).rstrip("/"),
        pm_event_path=g("PM_EVENT_PATH", d.pm_event_path),
        pm_market_path=g("PM_MARKET_PATH", d.pm_market_path),
        pm_book_path=g("PM_BOOK_PATH", d.pm_book_path),
        pm_book_is_up=_b("PM_BOOK_IS_UP", "true"),
        retention_days=int(g("RETENTION_DAYS", d.retention_days)),
        resolve_delay_s=float(g("RESOLVE_DELAY_S", d.resolve_delay_s)),
        log_level=g("LOG_LEVEL", d.log_level),
        daily_enabled=_b("DAILY_ENABLED", "true"),
        daily_size=float(g("DAILY_SIZE", d.daily_size)),
        daily_poll_s=float(g("DAILY_POLL_S", d.daily_poll_s)),
        daily_scan_interval_s=float(g("DAILY_SCAN_INTERVAL_S", d.daily_scan_interval_s)),
        daily_min_edge_bps=float(g("DAILY_MIN_EDGE_BPS", d.daily_min_edge_bps)),
        daily_no_entry_last_s=float(g("DAILY_NO_ENTRY_LAST_S", d.daily_no_entry_last_s)),
        daily_band_lo=float(g("DAILY_BAND_LO", d.daily_band_lo)),
        daily_band_hi=float(g("DAILY_BAND_HI", d.daily_band_hi)),
        daily_concurrency=int(g("DAILY_CONCURRENCY", d.daily_concurrency)),
        daily_days_ahead=int(g("DAILY_DAYS_AHEAD", d.daily_days_ahead)),
        daily_settle_tol_s=float(g("DAILY_SETTLE_TOL_S", d.daily_settle_tol_s)),
        daily_resolve_delay_s=float(g("DAILY_RESOLVE_DELAY_S", d.daily_resolve_delay_s)),
        weather_enabled=_b("WEATHER_ENABLED", "true"),
        weather_size=float(g("WEATHER_SIZE", d.weather_size)),
        weather_poll_s=float(g("WEATHER_POLL_S", d.weather_poll_s)),
        weather_scan_interval_s=float(g("WEATHER_SCAN_INTERVAL_S", d.weather_scan_interval_s)),
        weather_min_edge_bps=float(g("WEATHER_MIN_EDGE_BPS", d.weather_min_edge_bps)),
        weather_min_edge_bps_next=float(g("WEATHER_MIN_EDGE_BPS_NEXT", d.weather_min_edge_bps_next)),
        weather_days_ahead=int(g("WEATHER_DAYS_AHEAD", d.weather_days_ahead)),
        weather_max_stale_s=float(g("WEATHER_MAX_STALE_S", d.weather_max_stale_s)),
        weather_trusted_cities=g("WEATHER_TRUSTED_CITIES", d.weather_trusted_cities),
        weather_resolve_delay_s=float(g("WEATHER_RESOLVE_DELAY_S", d.weather_resolve_delay_s)),
        pm_gamma_base=g("PM_GAMMA_BASE", d.pm_gamma_base).rstrip("/"),
        pm_clob_base=g("PM_CLOB_BASE", d.pm_clob_base).rstrip("/"),
        quote_store_every_s=float(g("QUOTE_STORE_EVERY_S", d.quote_store_every_s)),
        quote_store_ladders=_b("QUOTE_STORE_LADDERS", "false"),
        signal_observe_every_s=float(g("SIGNAL_OBSERVE_EVERY_S", d.signal_observe_every_s)),
        daily_observe_every_s=float(g("DAILY_OBSERVE_EVERY_S", d.daily_observe_every_s)),
        weather_observe_every_s=float(g("WEATHER_OBSERVE_EVERY_S", d.weather_observe_every_s)),
        quotes_retention_hours=float(g("QUOTES_RETENTION_HOURS", d.quotes_retention_hours)),
        signals_retention_days=float(g("SIGNALS_RETENTION_DAYS", d.signals_retention_days)),
        db_max_mb=float(g("DB_MAX_MB", d.db_max_mb)),
        api_id=g("API_ID", ""),
        api_key=g("API_KEY", ""),
        port=int(g("PORT", d.port)),
        api_cors=g("API_CORS_ORIGINS", d.api_cors),
    )
