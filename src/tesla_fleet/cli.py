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


@app.command("report")
def report(roi_state: Path = typer.Option(Path(".roi_state.json"))) -> None:
    """Print the ROI / TCO report from saved state without polling."""
    from tesla_fleet.monitor import RoiState

    state = RoiState.load(roi_state)
    typer.echo(json.dumps(state.report(), indent=2))


if __name__ == "__main__":
    app()
