from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .timeutil import Instant, parse_instant

NY = ZoneInfo("America/New_York")
MIN_DATA_DATE = date(2025, 11, 3)


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime
    close: datetime


@dataclass(frozen=True)
class Quote:
    timestamp: Instant
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    raw: dict
    ordinal: int = 0

    @classmethod
    def from_api(cls, value: dict, ordinal: int = 0) -> "Quote":
        required = {"t", "bp", "ap", "bs", "as"}
        if not required <= value.keys():
            raise ValueError("quote missing required fields")
        return cls(parse_instant(value["t"]), Decimal(value["bp"]), Decimal(value["ap"]),
                   Decimal(value["bs"]), Decimal(value["as"]), value, ordinal)

    def structurally_usable(self, session: Session) -> bool:
        return (self.timestamp.dt.date() >= MIN_DATA_DATE and self.timestamp.dt >= session.open and
                self.timestamp.dt <= session.close and self.bid > 0 and self.ask > 0 and
                self.bid_size > 0 and self.ask_size > 0 and self.ask >= self.bid)


def parse_session(value: dict) -> Session:
    day = date.fromisoformat(value["date"])
    def local(v: str) -> datetime:
        # Alpaca calendar gives local HH:MM or HH:MM:SS; date supplies DST context.
        return datetime.combine(day, time.fromisoformat(v), NY).astimezone(UTC)
    return Session(day, local(value["open"]), local(value["close"]))


def select_quote(quotes: list[Quote], instant: datetime, session: Session,
                 lookback: timedelta = timedelta(minutes=2),
                 max_staleness: timedelta = timedelta(seconds=30)) -> Quote | None:
    delta = instant.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    target_ns = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
    lower = instant - lookback
    candidates = [q for q in quotes if q.structurally_usable(session) and
                  lower <= q.timestamp.dt and q.timestamp.ns <= target_ns]
    if not candidates:
        return None
    # max preserves later API record for equal nanosecond timestamps.
    q = max(candidates, key=lambda x: (x.timestamp.ns, x.ordinal))
    return q if instant - q.timestamp.dt <= max_staleness else None


def eligibility(submitted: datetime, session: Session, latency_ms: int) -> datetime | None:
    delay = timedelta(milliseconds=latency_ms)
    if submitted < session.open:
        return session.open + delay
    if submitted <= session.close:
        return submitted + delay
    return None
