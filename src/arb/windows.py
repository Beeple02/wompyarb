from __future__ import annotations

from datetime import datetime, timedelta, timezone

WINDOW_S = 900
KS_SERIES = {"btc": "KXBTC15M", "eth": "KXETH15M", "sol": "KXSOL15M", "xrp": "KXXRP15M"}


def current(now: datetime) -> tuple[datetime, datetime]:
    ts = int(now.timestamp())
    start = ts - ts % WINDOW_S
    s = datetime.fromtimestamp(start, tz=timezone.utc)
    return s, s + timedelta(seconds=WINDOW_S)


def pm_event_slug(asset: str, start: datetime) -> str:
    s = start.astimezone(timezone.utc)
    return f"{asset}-updown-15m-{s:%Y-%m-%d-%H%M}z"


def parse_ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
