from __future__ import annotations

from dataclasses import dataclass

Level = tuple[float, float]  # (price in dollars 0..1, quantity in contracts)


@dataclass
class MarketRef:
    venue: str
    ident: str  # kalshi: market ticker ; polymarket: market slug


@dataclass
class Book:
    """Order book normalised to what it costs to BUY each outcome.

    asks_up   : ladder (ascending price) to buy UP   (Kalshi YES / Polymarket Up)
    asks_down : ladder (ascending price) to buy DOWN (Kalshi NO  / Polymarket Down)
    """

    venue: str
    asks_up: list[Level]
    asks_down: list[Level]
    ts_recv: float
    ts_exchange: float | None = None

    def best(self, side: str) -> float | None:
        lad = self.asks_up if side == "up" else self.asks_down
        return lad[0][0] if lad else None

    def depth(self, side: str) -> float:
        lad = self.asks_up if side == "up" else self.asks_down
        return sum(q for _, q in lad)


def asks_from_bids(bids: list[Level]) -> list[Level]:
    """A bid at p for outcome X is an ask at 1-p for the opposite outcome."""
    out = [(round(1.0 - p, 6), q) for p, q in bids if q > 0]
    out.sort(key=lambda x: x[0])
    return out
