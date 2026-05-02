import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from tesla_fleet.monitor import (
    DriverState,
    Monitor,
    RoiState,
    _active_driver,
)


def test_active_driver_probe() -> None:
    assert _active_driver({"vehicle_state": {"active_driver_profile": "Carson"}}) == "Carson"
    assert _active_driver({"drive_state": {"active_driver_profile": "Bob"}}) == "Bob"
    assert _active_driver({}) is None


def test_roi_accumulates_miles_and_kwh(tmp_path: Path) -> None:
    state = RoiState()
    state.update(odometer=100.0, charge_energy_added=0.0)
    state.update(odometer=150.0, charge_energy_added=10.0)  # +50 mi, +10 kWh
    state.update(odometer=150.0, charge_energy_added=15.0)  # +5 kWh
    state.update(odometer=160.0, charge_energy_added=0.0)   # +10 mi, session reset
    state.update(odometer=160.0, charge_energy_added=8.0)   # new session +8 kWh
    assert state.total_miles == pytest.approx(60.0)
    assert state.total_kwh == pytest.approx(23.0)


def test_roi_report_math() -> None:
    state = RoiState(total_miles=280.0, total_kwh=70.0)
    r = state.report()
    # 280/28 = 10 gal * 4.39 = 43.90 ; 70 * 0.14 = 9.80 ; savings = 34.10
    assert r["gas_equivalent_cost_usd"] == 43.90
    assert r["electric_cost_usd"] == 9.80
    assert r["savings_usd"] == 34.10


def test_roi_persists(tmp_path: Path) -> None:
    p = tmp_path / "roi.json"
    s = RoiState(total_miles=12.5, total_kwh=4.0)
    s.save(p)
    loaded = RoiState.load(p)
    assert loaded.total_miles == 12.5
    assert loaded.total_kwh == 4.0


def test_driver_detects_hard_brake() -> None:
    d = DriverState()
    t0 = 1000.0
    assert d.detect(60.0, t0) is None  # baseline
    ev = d.detect(45.0, t0 + 1.0)  # -15 mph/s → hard brake
    assert ev and ev["type"] == "hard_brake"


def test_driver_detects_rapid_accel() -> None:
    d = DriverState()
    d.detect(0.0, 1000.0)
    ev = d.detect(20.0, 1001.0)  # +20 mph/s
    assert ev and ev["type"] == "rapid_accel"


def test_driver_no_event_within_thresholds() -> None:
    d = DriverState()
    d.detect(30.0, 1000.0)
    assert d.detect(33.0, 1001.0) is None  # +3 mph/s


@pytest.mark.asyncio
async def test_monitor_logs_carson_sample(tmp_path: Path) -> None:
    client = AsyncMock()
    client.vehicle_data.return_value = {
        "vehicle_state": {"active_driver_profile": "Carson", "odometer": 1000.0},
        "drive_state": {
            "speed": 35.0,
            "latitude": 32.75,
            "longitude": -97.33,
            "shift_state": "D",
            "speed_limit_mode": {"current_limit_mph": 45},
        },
        "charge_state": {"charge_energy_added": 0.0},
    }
    log = tmp_path / "log.json"
    roi = tmp_path / "roi.json"
    mon = Monitor(
        client=client, vin="5YJ", target_profile="Carson",
        log_path=log, roi_path=roi, poll_seconds=0,
    )
    await mon.run(iterations=1)

    lines = [json.loads(line) for line in log.read_text().splitlines()]
    samples = [r for r in lines if r["type"] == "driver_sample"]
    assert len(samples) == 1
    s = samples[0]
    assert s["driver"] == "Carson"
    assert s["speed_mph"] == 35.0
    assert s["speed_limit_mph"] == 45
    assert s["shift_state"] == "D"
    assert s["maps_url"] == "https://www.google.com/maps?q=32.75,-97.33"
    assert any(r["type"] == "roi_report" for r in lines)


@pytest.mark.asyncio
async def test_monitor_skips_other_drivers(tmp_path: Path) -> None:
    client = AsyncMock()
    client.vehicle_data.return_value = {
        "vehicle_state": {"active_driver_profile": "Alex", "odometer": 50.0},
        "drive_state": {"speed": 20.0},
        "charge_state": {},
    }
    log = tmp_path / "log.json"
    mon = Monitor(client=client, vin="5YJ", log_path=log,
                  roi_path=tmp_path / "r.json", poll_seconds=0)
    await mon.run(iterations=1)
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(r["type"] == "driver_sample" for r in lines)


@pytest.mark.asyncio
async def test_monitor_wakes_on_asleep(tmp_path: Path) -> None:
    client = AsyncMock()
    req = httpx.Request("GET", "https://x")
    client.vehicle_data.side_effect = httpx.HTTPStatusError(
        "asleep", request=req, response=httpx.Response(408, request=req)
    )
    client.wake_up.return_value = {"response": {}}
    mon = Monitor(client=client, vin="5YJ", log_path=tmp_path / "log.json",
                  roi_path=tmp_path / "r.json", poll_seconds=0)
    await mon.run(iterations=1)
    client.wake_up.assert_awaited_once_with("5YJ")
