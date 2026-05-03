"""Comparison vehicles for ROI / TCO calculations.

Each entry: real-world combined MPG and fuel grade. Premium-fuel uplift
over regular is sourced from the DFW EIA average historical spread
(~$0.65/gal).
"""
from __future__ import annotations

from dataclasses import dataclass

PREMIUM_UPLIFT_USD = 0.65  # DFW historical regular→premium spread


@dataclass(frozen=True)
class ComparisonVehicle:
    key: str
    name: str
    mpg: float
    fuel: str  # "regular" | "premium"
    note: str = ""


# Subject + comparisons. The Model 3 entry is the "current" vehicle and
# excluded from comparison but kept here for documentation.
COMPARISONS: list[ComparisonVehicle] = [
    ComparisonVehicle(
        key="generic_sedan",
        name="Generic 28 MPG sedan",
        mpg=28.0,
        fuel="regular",
        note="Original spec baseline",
    ),
    ComparisonVehicle(
        key="bronco_wildtrak_2dr",
        name="2023 Bronco Wildtrak 2-door (2.7L V6)",
        mpg=17.0,
        fuel="regular",
        note="EPA 16 city / 17 hwy",
    ),
    ComparisonVehicle(
        key="defender_110_3l",
        name="2024 Defender 110 (3.0 inline-6)",
        mpg=17.0,
        fuel="premium",
        note="EPA 17 combined; premium fuel required",
    ),
]


def fuel_price(ts: float, fuel: str, regular_price: float) -> float:
    """Adjust the DFW regular price for premium-fuel vehicles."""
    if fuel == "premium":
        return round(regular_price + PREMIUM_UPLIFT_USD, 3)
    return regular_price
