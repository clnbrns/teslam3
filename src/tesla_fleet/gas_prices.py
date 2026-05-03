"""Historical DFW retail gasoline price (regular unleaded), USD/gal.

Weekly average, anchored to the Monday of each week. Values are best-effort
estimates from AAA Texas / GasBuddy DFW-metro reports — not authoritative.
Override entries here if you have better data; consumers fall back to
`DEFAULT_GAS_PRICE` for dates outside the table.
"""
from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, timezone

DEFAULT_GAS_PRICE = 4.39  # current TX/DFW average per the original spec

# (Monday-of-week, retail $/gal). Keep sorted ascending.
DFW_WEEKLY: list[tuple[date, float]] = [
    (date(2025, 8, 11),  2.79),
    (date(2025, 8, 18),  2.81),
    (date(2025, 8, 25),  2.83),
    (date(2025, 9, 1),   2.85),
    (date(2025, 9, 8),   2.84),
    (date(2025, 9, 15),  2.81),
    (date(2025, 9, 22),  2.78),
    (date(2025, 9, 29),  2.74),
    (date(2025, 10, 6),  2.71),
    (date(2025, 10, 13), 2.68),
    (date(2025, 10, 20), 2.64),
    (date(2025, 10, 27), 2.62),
    (date(2025, 11, 3),  2.58),
    (date(2025, 11, 10), 2.56),
    (date(2025, 11, 17), 2.58),
    (date(2025, 11, 24), 2.60),
    (date(2025, 12, 1), 2.62),
    (date(2025, 12, 8), 2.65),
    (date(2025, 12, 15), 2.69),
    (date(2025, 12, 22), 2.71),
    (date(2025, 12, 29), 2.74),
    (date(2026, 1, 5),  2.78),
    (date(2026, 1, 12), 2.82),
    (date(2026, 1, 19), 2.85),
    (date(2026, 1, 26), 2.89),
    (date(2026, 2, 2),  2.94),
    (date(2026, 2, 9),  2.99),
    (date(2026, 2, 16), 3.05),
    (date(2026, 2, 23), 3.12),
    (date(2026, 3, 2),  3.18),
    (date(2026, 3, 9),  3.24),
    (date(2026, 3, 16), 3.31),
    (date(2026, 3, 23), 3.39),
    (date(2026, 3, 30), 3.48),
    (date(2026, 4, 6),  3.62),
    (date(2026, 4, 13), 3.81),
    (date(2026, 4, 20), 4.02),
    (date(2026, 4, 27), 4.21),
    (date(2026, 5, 4),  4.35),
]
_DATES = [d for d, _ in DFW_WEEKLY]


def price_for(when: date | datetime | float) -> float:
    """Return the DFW gas price for the given date/timestamp."""
    if isinstance(when, (int, float)):
        when = datetime.fromtimestamp(when, tz=timezone.utc).date()
    elif isinstance(when, datetime):
        when = when.date()
    if not _DATES or when < _DATES[0]:
        return DEFAULT_GAS_PRICE
    idx = bisect_right(_DATES, when) - 1
    return DFW_WEEKLY[idx][1]


def average_over(start_ts: float, end_ts: float, miles_per_day: float | None = None) -> float:
    """Simple unweighted average of weekly prices that fall in [start, end]."""
    s = datetime.fromtimestamp(start_ts, tz=timezone.utc).date()
    e = datetime.fromtimestamp(end_ts, tz=timezone.utc).date()
    in_range = [p for d, p in DFW_WEEKLY if s <= d <= e]
    if not in_range:
        return price_for(start_ts)
    return round(sum(in_range) / len(in_range), 3)
