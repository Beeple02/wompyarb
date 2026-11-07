"""Buffer circulaire des logs en mémoire, exposé via /api/logs."""
from __future__ import annotations

import collections
import itertools
import logging

_buf: collections.deque = collections.deque(maxlen=3000)
_ids = itertools.count(1)
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            _buf.append({"id": next(_ids), "ts": record.created, "level": record.levelname,
                         "name": record.name, "msg": msg})
        except Exception:
            pass


def install() -> None:
    logging.getLogger().addHandler(RingHandler(logging.INFO))


def tail(limit: int = 200, min_level: str = "INFO", contains: str | None = None,
         after_id: int = 0) -> list[dict]:
    lvl = _LEVELS.get(min_level.upper(), 20)
    out = [e for e in list(_buf)
           if e["id"] > after_id and _LEVELS.get(e["level"], 0) >= lvl
           and (not contains or contains.lower() in e["msg"].lower())]
    return out[-limit:]
