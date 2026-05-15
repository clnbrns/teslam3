"""Tesla Fleet CLI."""
from __future__ import annotations

import asyncio
import json
import logging

import typer

from pathlib import Path

from tesla_fleet.auth import build_authorize_url
from tesla_fleet.client import TeslaFleetClient
from tesla_fleet.config import Settings
from tesla_fleet.monitor import Monitor
from tesla_fleet.tokens import TokenStore, exchange_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

app = typer.Typer(no_args_is_help=True)
auth_app = typer.Typer(no_args_is_help=True, help="OAuth flows")
vehicles_app = typer.Typer(no_args_is_help=True, help="Vehicle ops")
app.add_typer(auth_app, name="auth")
app.add_typer(vehicles_app, name="vehicles")


def _settings() -> Settings:
    return Settings()


def _client_ctx() -> tuple[TeslaFleetClient, TokenStore]:
    settings = _settings()
    store = TokenStore(settings.token_store_path)
    token = store.load()
    if token is None:
        raise typer.BadParameter("No token. Run `tesla-fleet auth login` first.")
    return TeslaFleetClient(settings, token, store), store


@auth_app.command("url")
def auth_url() -> None:
    """Print the authorization URL to visit."""
    url, state = build_authorize_url(_settings())
    typer.echo(f"State: {state}")
    typer.echo(url)


@auth_app.command("exchange")
def auth_exchange(code: str) -> None:
    """Exchange an authorization code for tokens."""
    settings = _settings()

    async def _run() -> None:
        token = await exchange_code(code, settings)
        TokenStore(settings.token_store_path).save(token)
        typer.echo(f"Saved token to {settings.token_store_path}")

    asyncio.run(_run())


@auth_app.command("login")
def auth_login() -> None:
    """Interactive: print URL, prompt for the returned ?code=..."""
    url, _ = build_authorize_url(_settings())
    typer.echo(f"Visit:\n{url}\n")
    code = typer.prompt("Paste the `code` query param from the redirect URL")
    auth_exchange(code)


@vehicles_app.command("list")
def vehicles_list() -> None:
    async def _run() -> None:
        client, _ = _client_ctx()
        async with client:
            for v in await client.list_vehicles():
                typer.echo(f"{v.get('vin')}\t{v.get('display_name')}\t{v.get('state')}")

    asyncio.run(_run())


@vehicles_app.command("data")
def vehicles_data(vin: str) -> None:
    async def _run() -> None:
        client, _ = _client_ctx()
        async with client:
            typer.echo(json.dumps(await client.vehicle_data(vin), indent=2))

    asyncio.run(_run())


@vehicles_app.command("wake")
def vehicles_wake(vin: str) -> None:
    async def _run() -> None:
        client, _ = _client_ctx()
        async with client:
            typer.echo(json.dumps(await client.wake_up(vin), indent=2))

    asyncio.run(_run())


@vehicles_app.command("cmd")
def vehicles_cmd(vin: str, name: str, payload: str = "{}") -> None:
    """Send a raw command, e.g. `vehicles cmd <vin> set_charge_limit '{"percent": 80}'`."""
    async def _run() -> None:
        client, _ = _client_ctx()
        async with client:
            typer.echo(json.dumps(await client.command(vin, name, json.loads(payload)), indent=2))

    asyncio.run(_run())


@app.command("monitor")
def monitor(
    vin: str,
    profile: str = typer.Option("Carson", help="Target driver profile name"),
    interval: float = typer.Option(30.0, help="Poll interval (seconds)"),
    log_file: Path = typer.Option(Path("monitoring_log.json"), help="JSON-lines event log"),
    roi_state: Path = typer.Option(Path(".roi_state.json"), help="Persistent ROI state"),
    iterations: int = typer.Option(0, help="Stop after N polls (0 = run forever)"),
) -> None:
    """Poll a vehicle, log driver activity for a target profile, and track ROI."""

    async def _run() -> None:
        client, _ = _client_ctx()
        async with client:
            mon = Monitor(
                client=client,
                vin=vin,
                target_profile=profile,
                log_path=log_file,
                roi_path=roi_state,
                poll_seconds=interval,
            )
            await mon.run(iterations=iterations or None)
            mon.print_report()

    asyncio.run(_run())


telemetry_app = typer.Typer(no_args_is_help=True, help="Fleet Telemetry streaming")
app.add_typer(telemetry_app, name="telemetry")


@telemetry_app.command("register")
def telemetry_register(
    vin: str,
    hostname: str = typer.Option("", help="FTS hostname the car will push to"),
    proxy: str = typer.Option("", help="Vehicle Command Proxy URL (required for new configs)"),
) -> None:
    """Register a telemetry streaming config for VIN at hostname.

    Tesla requires telemetry-config calls to be signed via the Vehicle
    Command Proxy. Pass `--proxy https://vcp.burnsbuilt.co` (or set
    VEHICLE_COMMAND_PROXY_URL).
    """
    from tesla_fleet import telemetry as t

    async def _run() -> None:
        client, _ = _client_ctx()
        async with client:
            host = hostname or _settings().public_hostname
            proxy_url = proxy or _settings().vehicle_command_proxy_url
            if not host:
                raise typer.BadParameter("Pass --hostname or set TESLA_PUBLIC_HOSTNAME")
            if not proxy_url:
                typer.echo("warning: no --proxy; Tesla will reject with 400", err=True)
            typer.echo(json.dumps(await t.register(client, host, vin, proxy_url or None), indent=2))

    asyncio.run(_run())


@telemetry_app.command("unregister")
def telemetry_unregister(
    vin: str,
    proxy: str = typer.Option("", help="Vehicle Command Proxy URL"),
) -> None:
    from tesla_fleet import telemetry as t

    async def _run() -> None:
        client, _ = _client_ctx()
        async with client:
            proxy_url = proxy or _settings().vehicle_command_proxy_url
            typer.echo(json.dumps(await t.unregister(client, vin, proxy_url or None), indent=2))

    asyncio.run(_run())


@app.command("seed-attention")
def seed_attention(
    vin: str = typer.Option("5YJ3E1ETXRF901558"),
    days: int = typer.Option(7),
) -> None:
    """Generate plausible cabin-camera attention events for the last N days.

    Useful to demo the /attention dashboard before Tesla SW 2026.8 telemetry
    actually starts flowing. Idempotent — re-runs replace nothing on PK conflict.
    """
    import random
    import time
    from tesla_fleet import db

    random.seed(42)
    db.init()
    now = time.time()
    written = 0
    profiles = [
        # (driver, gaze_per_day, phone_per_day, drowsy_per_day, score_target)
        ("Colin",   2,  0,  0,  "A"),  # uses FSD heavily; eyes on road
        ("Carson",  6,  3,  0,  "C"),  # phone occasionally
        ("Lindsey", 4,  1,  1,  "B"),
    ]
    with db.connect() as conn:
        for driver, gz, ph, dr, _ in profiles:
            for d in range(days):
                base = now - (d * 86_400) - random.randint(0, 30_000)
                for kind, count in (("gaze_away", gz), ("phone_use", ph), ("drowsy", dr)):
                    for i in range(random.randint(max(0, count - 2), count + 2)):
                        ev = {
                            "vin": vin, "ts": base + (i * 137) + random.uniform(0, 3.0),
                            "driver": driver, "verified_driver": driver, "kind": kind,
                            "duration_s": round(random.uniform(1.5, 6.0), 1),
                            "severity": random.choice(["low", "low", "medium", "high"]),
                            "speed_mph": round(random.uniform(20, 75), 1),
                            "payload": {"source": "seed-attention", "synthetic": True},
                        }
                        if db.record_attention(conn, ev):
                            written += 1
        # One verification mismatch to demonstrate the panel.
        for i in range(3):
            ev = {
                "vin": vin, "ts": now - (i * 3600), "driver": "Colin",
                "verified_driver": "Carson", "kind": "driver_swap",
                "speed_mph": 12.0,
                "payload": {"source": "seed-attention"},
            }
            db.record_attention(conn, ev)
    typer.echo(f"Wrote {written} synthetic attention events")


@app.command("report")
def report(roi_state: Path = typer.Option(Path(".roi_state.json"))) -> None:
    """Print the ROI / TCO report from saved state without polling."""
    from tesla_fleet.monitor import RoiState

    state = RoiState.load(roi_state)
    typer.echo(json.dumps(state.report(), indent=2))


if __name__ == "__main__":
    app()
