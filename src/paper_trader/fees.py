from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_CEILING

CENT = Decimal("0.01")
TAF_SCHEDULE = {
    2025: (Decimal("0.000166"), Decimal("8.30")),
    2026: (Decimal("0.000195"), Decimal("9.79")),
    2027: (Decimal("0.000232"), Decimal("11.61")),
    2028: (Decimal("0.000240"), Decimal("12.05")),
    2029: (Decimal("0.000249"), Decimal("12.50")),
}


def sale_fee_breakdown(value: Decimal, shares: int, day: date) -> tuple[dict[str, Decimal], list[str]]:
    warnings: list[str] = []
    if date(2025, 11, 3) <= day <= date(2026, 4, 3):
        sec = Decimal(0)
    elif date(2026, 4, 4) <= day <= date(2026, 12, 31):
        sec = (value * Decimal("0.00002060")).quantize(CENT, rounding=ROUND_CEILING)
    else:
        sec = (value * Decimal("0.00002060")).quantize(CENT, rounding=ROUND_CEILING)
        warnings.append("SEC Section 31 estimate uses the 2026-04-04 rate outside its verified effective range")
    if day.year in TAF_SCHEDULE:
        taf_rate, taf_cap = TAF_SCHEDULE[day.year]
    else:
        taf_rate, taf_cap = Decimal("0.000195"), Decimal("9.79")
        warnings.append("FINRA TAF estimate uses the 2026 rate outside the published 2025-2029 schedule")
    # FINRA exempts sales executed below the applicable per-share TAF rate.
    if value / Decimal(shares) < taf_rate:
        taf = Decimal("0.00")
    else:
        taf = min(taf_cap, Decimal(shares) * taf_rate)
    return {"sec_section_31": sec, "finra_taf": taf}, warnings


def sale_fees(value: Decimal, shares: int, day: date) -> tuple[Decimal, list[str]]:
    breakdown, warnings = sale_fee_breakdown(value, shares, day)
    return sum(breakdown.values(), Decimal(0)), warnings


def commission(shares: int, per_order: Decimal, per_share: Decimal) -> Decimal:
    return per_order + Decimal(shares) * per_share


def commission_breakdown(shares: int, per_order: Decimal, per_share: Decimal) -> dict[str, Decimal]:
    return {
        "commission_per_order": per_order,
        "commission_per_share": Decimal(shares) * per_share,
    }
