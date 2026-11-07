from __future__ import annotations

import os
from dataclasses import dataclass, field


def _b(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    database_url: str = ""
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

    kalshi_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    pm_base: str = "https://gateway.polymarket.us"
    pm_event_path: str = "/v1/events/slug/{slug}"
    pm_market_path: str = "/v1/markets/slug/{slug}"
    pm_book_path: str = "/v1/markets/{slug}/book"
    pm_book_is_up: bool = True

    retention_days: int = 7
    resolve_delay_s: float = 75
    log_level: str = "INFO"

    api_id: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    port: int = 8080
    api_cors: str = "*"


def load() -> Config:
    g = os.getenv
    d = Config()
    return Config(
        database_url=g("DATABASE_URL", ""),
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
        kalshi_base=g("KALSHI_BASE", d.kalshi_base).rstrip("/"),
        pm_base=g("PM_GATEWAY_BASE", d.pm_base).rstrip("/"),
        pm_event_path=g("PM_EVENT_PATH", d.pm_event_path),
        pm_market_path=g("PM_MARKET_PATH", d.pm_market_path),
        pm_book_path=g("PM_BOOK_PATH", d.pm_book_path),
        pm_book_is_up=_b("PM_BOOK_IS_UP", "true"),
        retention_days=int(g("RETENTION_DAYS", d.retention_days)),
        resolve_delay_s=float(g("RESOLVE_DELAY_S", d.resolve_delay_s)),
        log_level=g("LOG_LEVEL", d.log_level),
        api_id=g("API_ID", ""),
        api_key=g("API_KEY", ""),
        port=int(g("PORT", d.port)),
        api_cors=g("API_CORS_ORIGINS", d.api_cors),
    )
