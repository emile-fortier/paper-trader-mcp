from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import re

_RFC3339 = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)$")


@dataclass(frozen=True)
class Instant:
    raw: str
    ns: int
    dt: datetime


def parse_instant(raw: str) -> Instant:
    m = _RFC3339.fullmatch(raw)
    if not m:
        raise ValueError(f"invalid RFC3339 timestamp: {raw}")
    frac = (m.group(2) or "").ljust(9, "0")
    iso = m.group(1) + ("." + frac[:6] if frac else "") + ("+00:00" if m.group(3) == "Z" else m.group(3))
    dt = datetime.fromisoformat(iso).astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    whole = dt.replace(microsecond=0) - epoch
    seconds = whole.days * 86_400 + whole.seconds
    ns = seconds * 1_000_000_000 + int(frac or "0")
    return Instant(raw, ns, dt)


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def safe_cutoff(now: datetime) -> datetime:
    return now.astimezone(UTC) - timedelta(minutes=16)
