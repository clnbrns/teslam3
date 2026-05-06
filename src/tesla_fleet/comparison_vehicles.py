"""Comparison vehicles for ROI / TCO calculations.

Each entry: real-world combined MPG and fuel grade. Premium-fuel uplift
over regular is sourced from the DFW EIA average historical spread
(~$0.65/gal).
"""
from __future__ import annotations

from dataclasses import dataclass

PREMIUM_UPLIFT_USD = 0.65  # DFW historical regular→premium spread
DIESEL_UPLIFT_USD = 0.55   # DFW 2025–26 retail diesel typically runs ~$0.55/gal
                           # above regular unleaded; varies seasonally

# Tesla Model 3 routine maintenance: tires, cabin filter, brake fluid,
# A/C desiccant. AAA/Edmunds put EV total at ~3-5¢/mi; we use 4¢.
TESLA_MAINT_PER_MI = 0.04


@dataclass(frozen=True)
class ComparisonVehicle:
    key: str
    name: str
    mpg: float                 # 0 for electric vehicles
    fuel: str                  # "regular" | "premium" | "electric"
    maint_per_mi: float        # USD per mile, scheduled maintenance
    mi_per_kwh: float = 0.0    # only used when fuel == "electric"
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
        name="2023 Ford Bronco Wildtrak 2-door (2.7L V6)",
        mpg=17.0,
        fuel="regular",
        maint_per_mi=0.13,
        note="Former daily · all-terrain tires drive cost up",
    ),
    ComparisonVehicle(
        key="defender_110_3l",
        name="2024 Land Rover Defender 110 (3.0 inline-6)",
        mpg=17.0,
        fuel="premium",
        maint_per_mi=0.18,
        note="Lindsey's car · premium fuel · luxury parts/service rates",
    ),
    ComparisonVehicle(
        key="bmw_m3_2015",
        name="2015 BMW M3 (F80, 3.0L twin-turbo I6)",
        mpg=19.0,
        fuel="premium",
        maint_per_mi=0.20,
        note="Former vehicle · S55 carbon canister + brakes are pricey",
    ),
    ComparisonVehicle(
        key="lexus_lx470_2006",
        name="2006 Lexus LX470 (4.7L V8)",
        mpg=13.0,
        fuel="premium",
        maint_per_mi=0.18,
        note="Former vehicle · 100-series Land Cruiser platform; thirsty V8",
    ),
    ComparisonVehicle(
        key="model3_2019_lr",
        name="2019 Tesla Model 3 AWD Long Range",
        mpg=0.0,
        fuel="electric",
        maint_per_mi=0.04,
        mi_per_kwh=4.0,
        note="Former vehicle · efficient AWD; ~30% fewer kWh per mile than the Performance",
    ),
    ComparisonVehicle(
        key="tahoe_z71_2024",
        name="2024 Chevrolet Tahoe Z71 (5.3L V8)",
        mpg=17.0,
        fuel="regular",
        maint_per_mi=0.16,
        note="EPA 16/20 combined ~17 · large SUV maintenance",
    ),
    ComparisonVehicle(
        key="f250_67ho_2026",
        name="2026 Ford F-250 (6.7L Power Stroke HO Diesel)",
        mpg=15.0,
        fuel="diesel",
        maint_per_mi=0.22,
        note=(
            "500 hp / 1,200 lb-ft · DEF + fuel-filter + 13-qt oil change + "
            "DPF regen drive maintenance up; ~$0.55/gal diesel premium over regular"
        ),
    ),
]


def fuel_price(ts: float, fuel: str, regular_price: float) -> float:
    """Adjust the DFW regular price for premium-fuel or diesel vehicles."""
    if fuel == "premium":
        return round(regular_price + PREMIUM_UPLIFT_USD, 3)
    if fuel == "diesel":
        return round(regular_price + DIESEL_UPLIFT_USD, 3)
    return regular_price
