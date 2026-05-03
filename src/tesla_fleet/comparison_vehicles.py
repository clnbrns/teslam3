"""Comparison vehicles for ROI / TCO calculations.

Each entry: real-world combined MPG and fuel grade. Premium-fuel uplift
over regular is sourced from the DFW EIA average historical spread
(~$0.65/gal).
"""
from __future__ import annotations

from dataclasses import dataclass

PREMIUM_UPLIFT_USD = 0.65  # DFW historical regular→premium spread

# Tesla Model 3 routine maintenance: tires, cabin filter, brake fluid,
# A/C desiccant. AAA/Edmunds put EV total at ~3-5¢/mi; we use 4¢.
TESLA_MAINT_PER_MI = 0.04


@dataclass(frozen=True)
class ComparisonVehicle:
    key: str
    name: str
    mpg: float
    fuel: str  # "regular" | "premium"
    maint_per_mi: float  # USD per mile, scheduled maintenance
    note: str = ""


# Maintenance numbers anchored to Edmunds True Cost to Own + AAA Your
# Driving Costs (2024-2025). Includes oil/filter, brake pads/rotors,
# spark plugs, transmission service, coolant, tires, scheduled inspections.
COMPARISONS: list[ComparisonVehicle] = [
    ComparisonVehicle(
        key="generic_sedan",
        name="Generic 28 MPG sedan",
        mpg=28.0,
        fuel="regular",
        maint_per_mi=0.08,
        note="Camry/Accord-class baseline",
    ),
    ComparisonVehicle(
        key="bronco_wildtrak_2dr",
        name="2023 Bronco Wildtrak 2-door (2.7L V6)",
        mpg=17.0,
        fuel="regular",
        maint_per_mi=0.13,
        note="EPA 16 city / 17 hwy · all-terrain tires drive cost up",
    ),
    ComparisonVehicle(
        key="defender_110_3l",
        name="2024 Defender 110 (3.0 inline-6)",
        mpg=17.0,
        fuel="premium",
        maint_per_mi=0.18,
        note="EPA 17 combined · premium fuel · luxury parts/service rates",
    ),
]


def fuel_price(ts: float, fuel: str, regular_price: float) -> float:
    """Adjust the DFW regular price for premium-fuel vehicles."""
    if fuel == "premium":
        return round(regular_price + PREMIUM_UPLIFT_USD, 3)
    return regular_price
