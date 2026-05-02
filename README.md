# tesla-fleet

Python client + FastAPI service for the Tesla Fleet API.

## Features
- OAuth 2.0 authorization code flow (partner + third-party app)
- Token storage and refresh
- Vehicle list, status (charge, climate, location), and commands (wake, lock/unlock, climate start/stop, charge start/stop)
- CLI (`tesla-fleet`) and HTTP service (`uvicorn tesla_fleet.api:app`)

## Prereqs
1. Register a third-party app at https://developer.tesla.com/
2. Host your public key at `https://<your-domain>/.well-known/appspecific/com.tesla.3p.public-key.pem`
3. Set env vars in `.env` (see `.env.example`)

## Quick start
```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in credentials
tesla-fleet auth login
tesla-fleet vehicles list
```

## Endpoints used
Base: `https://fleet-api.prd.na.vn.cloud.tesla.com` (NA) — see `tesla_fleet.config.REGIONS` for EU/APAC.

- `POST /oauth2/v3/token` — token exchange & refresh
- `GET /api/1/vehicles` — list vehicles
- `GET /api/1/vehicles/{vin}/vehicle_data` — full state
- `POST /api/1/vehicles/{vin}/command/{name}` — commands (signed via vehicle-command proxy for newer vehicles)

## Notes
Newer vehicles (post-2021 Model S/X, all Model 3/Y after the 2024 protocol change) require commands to be signed and sent via Tesla's `vehicle-command` HTTP proxy. This client supports both signed and unsigned paths; configure `VEHICLE_COMMAND_PROXY_URL` if running the proxy.
