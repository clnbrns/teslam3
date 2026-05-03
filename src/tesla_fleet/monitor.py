"""Vehicle monitoring + ROI/TCO tracking.

Use case 1 — Carson driver-activity monitor:
    Polls vehicle_data, detects when the active driver profile matches a
    target name, and emits structured events (speed/limit, GPS, shift state,
    hard braking / rapid acceleration) to a JSON-lines log.

Use case 2 — Fleet ROI vs. gas ICE:
    Tracks odometer deltas (miles) and charge_energy_added deltas (kWh)
    across the polling loop and prints a TCO summary.

Notes / assumptions:
- Tesla's Fleet API does not expose a stable `active_driver_profile` field
  on every firmware. We probe a list of likely paths and fall back to the
  Tesla profile ID if a name lookup fails. Override via `--profile-path`.
- `speed_limit` reflects the in-vehicle speed-limit-mode setting (not the
  road limit, which Fleet API does not return). Real-time speed comes from
  `drive_state.speed` (mph).
- Wake-on-asleep is best-effort: we send `wake_up` and resume polling on
  the next tick rather than blocking until online.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from tesla_fleet.client import TeslaFleetClient

logger = logging.getLogger(__name__)

# --- ROI globals (per spec) ---
GAS_PRICE_PER_GAL = 4.39
GAS_MPG_BASELINE = 28.0
ELEC_PRICE_PER_KWH = 0.14

# --- Driver-event thresholds (mph/s ≈ 0.45 g) ---
HARD_BRAKE_MPHS = -7.0
RAPID_ACCEL_MPHS = 7.0

# Probe order for the active-driver field across firmware variants.
DRIVER_PROFILE_PATHS: tuple[tuple[str, ...], ...] = (
    ("vehicle_state", "active_driver_profile"),
    ("vehicle_config", "active_driver_profile"),
    ("drive_state", "active_driver_profile"),
    ("vehicle_state", "driver_profile"),
)


def _dig(d: dict, path: tuple[str, ...]) -> Any:
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _active_driver(data: dict) -> str | None:
    for p in DRIVER_PROFILE_PATHS:
        v = _dig(data, p)
        if v:
            return str(v)
    return None


@dataclass
class RoiState:
    """Persisted across runs so deltas survive restarts."""

    last_odometer: float | None = None
    last_charge_energy_added: float | None = None
    total_miles: float = 0.0
    total_kwh: float = 0.0

    @classmethod
    def load(cls, path: Path) -> "RoiState":
        if path.exists():
            return cls(**json.loads(path.read_text()))
        return cls()

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.__dict__, indent=2))

    def update(self, odometer: float | None, charge_energy_added: float | None) -> None:
        if odometer is not None:
            if self.last_odometer is not None and odometer >= self.last_odometer:
                self.total_miles += odometer - self.last_odometer
            self.last_odometer = odometer
        if charge_energy_added is not None:
            prev = self.last_charge_energy_added
            if prev is not None:
                # In-session: monotonically increasing → accumulate delta.
                # Session reset (drops to ~0): treat the prior peak as banked.
                if charge_energy_added >= prev:
                    self.total_kwh += charge_energy_added - prev
                # else: session ended, prior peak already accounted for.
            self.last_charge_energy_added = charge_energy_added

    def report(self, gas_price: float = GAS_PRICE_PER_GAL,
               elec_price: float = ELEC_PRICE_PER_KWH,
               mpg: float = GAS_MPG_BASELINE,
               gas_price_label: str = "TX average") -> dict:
        gas_cost = (self.total_miles / mpg) * gas_price
        elec_cost = self.total_kwh * elec_price
        savings = gas_cost - elec_cost
        return {
            "total_miles": round(self.total_miles, 2),
            "total_kwh": round(self.total_kwh, 3),
            "gas_equivalent_cost_usd": round(gas_cost, 2),
            "electric_cost_usd": round(elec_cost, 2),
            "savings_usd": round(savings, 2),
            "assumptions": {
                "gas_price_per_gal": gas_price,
                "gas_price_source": gas_price_label,
                "mpg_baseline": mpg,
                "elec_price_per_kwh": elec_price,
            },
        }


@dataclass
class DriverState:
    """Tracks deltas needed for accel/brake detection."""

    last_speed_mph: float | None = None
    last_ts: float | None = None
    events: int = 0

    def detect(self, speed_mph: float | None, now: float) -> dict | None:
        if speed_mph is None:
            return None
        event: dict | None = None
        if self.last_speed_mph is not None and self.last_ts is not None:
            dt = now - self.last_ts
            if dt > 0:
                d_mphs = (speed_mph - self.last_speed_mph) / dt
                if d_mphs <= HARD_BRAKE_MPHS:
                    event = {"type": "hard_brake", "delta_mph_per_s": round(d_mphs, 2)}
                elif d_mphs >= RAPID_ACCEL_MPHS:
                    event = {"type": "rapid_accel", "delta_mph_per_s": round(d_mphs, 2)}
        self.last_speed_mph = speed_mph
        self.last_ts = now
        if event:
            self.events += 1
        return event


@dataclass
class Monitor:
    client: TeslaFleetClient
    vin: str
    target_profile: str = "Carson"
    log_path: Path = field(default_factory=lambda: Path("monitoring_log.json"))
    roi_path: Path = field(default_factory=lambda: Path(".roi_state.json"))
    poll_seconds: float = 30.0
    driver: DriverState = field(default_factory=DriverState)
    roi: RoiState = field(init=False)

    def __post_init__(self) -> None:
        self.roi = RoiState.load(self.roi_path)

    def _emit(self, record: dict) -> None:
        record["ts"] = time.time()
        with self.log_path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        logger.info("event %s", record.get("type", "sample"))

    async def _fetch(self) -> dict | None:
        try:
            return await self.client.vehicle_data(self.vin)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # 408 (Tesla) / 404 commonly mean the car is asleep.
            if status in (404, 408):
                logger.info("vehicle asleep — sending wake_up")
                try:
                    await self.client.wake_up(self.vin)
                except httpx.HTTPError:
                    logger.exception("wake_up failed")
                return None
            logger.error("vehicle_data %s: %s", status, e.response.text[:200])
            return None
        except httpx.HTTPError:
            logger.exception("vehicle_data network error")
            return None

    def _process(self, data: dict) -> None:
        now = time.time()
        drive = data.get("drive_state") or {}
        veh = data.get("vehicle_state") or {}
        charge = data.get("charge_state") or {}

        odometer = veh.get("odometer")
        energy_added = charge.get("charge_energy_added")
        self.roi.update(odometer, energy_added)

        active = _active_driver(data)
        if active and active.lower() == self.target_profile.lower():
            speed = drive.get("speed")  # mph or None when parked
            lat = drive.get("latitude")
            lon = drive.get("longitude")
            shift = drive.get("shift_state")  # P/R/N/D or None
            speed_limit = (drive.get("speed_limit_mode") or {}).get("current_limit_mph")

            event = self.driver.detect(speed, now)

            self._emit({
                "type": "driver_sample",
                "vin": self.vin,
                "driver": active,
                "speed_mph": speed,
                "speed_limit_mph": speed_limit,
                "shift_state": shift,
                "gps": {"lat": lat, "lon": lon} if lat is not None else None,
                "maps_url": (
                    f"https://www.google.com/maps?q={lat},{lon}" if lat is not None else None
                ),
                "event": event,
            })

    async def run(self, iterations: int | None = None) -> None:
        i = 0
        try:
            while iterations is None or i < iterations:
                data = await self._fetch()
                if data is not None:
                    self._process(data)
                self.roi.save(self.roi_path)
                i += 1
                if iterations is None or i < iterations:
                    await asyncio.sleep(self.poll_seconds)
        finally:
            self.roi.save(self.roi_path)
            self._emit({"type": "roi_report", **self.roi.report()})

    def print_report(self) -> None:
        report = self.roi.report()
        print("=== Total Cost of Ownership vs. Gas ICE Equivalent ===")
        print(f"  Miles driven:        {report['total_miles']:>10,.2f} mi")
        print(f"  Energy charged:      {report['total_kwh']:>10,.3f} kWh")
        print(f"  Gas-equivalent cost: ${report['gas_equivalent_cost_usd']:>9,.2f}")
        print(f"  Electric cost:       ${report['electric_cost_usd']:>9,.2f}")
        print(f"  Savings:             ${report['savings_usd']:>9,.2f}")
