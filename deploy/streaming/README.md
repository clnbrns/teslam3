# Fleet Telemetry Streaming Setup

Eliminates polling entirely. The car pushes telemetry directly to us
over its cellular/WiFi connection — buffers locally when offline,
flushes when it reconnects. Zero Tesla API quota cost.

## Architecture

```
    +------------------+    cellular / WiFi (TLS, protobuf)
    |  Tesla Model 3   |  ─────────────────────────────────►
    +------------------+                                   │
                                                           ▼
                                       +----------------------------+
                                       | fts.burnsbuilt.co          |
                                       | Fleet Telemetry Server     |
                                       | (Tesla Go binary)          |
                                       +----------------------------+
                                                           │ HTTP POST
                                                           ▼
                                       +----------------------------+
                                       | tesla.burnsbuilt.co        |
                                       | /telemetry endpoint        |
                                       | → SQLite                   |
                                       +----------------------------+

  Separately, telemetry-config registration goes through the
  Vehicle Command Proxy (which signs requests with our partner key):

                        tesla-fleet CLI / web UI
                                   │
                                   ▼ HTTPS
                       +----------------------------+
                       | vcp.burnsbuilt.co          |
                       | Vehicle Command Proxy      |
                       | (Tesla Go binary)          |
                       +----------------------------+
                                   │ signed request
                                   ▼
                            Tesla Fleet API
```

## What you need to deploy

Two new Railway services in the same project as the FastAPI app:

| Service | Source | What it does |
|---|---|---|
| `vcp` | `deploy/streaming/vcp.Dockerfile` | Signs telemetry-config registrations + vehicle commands |
| `fts` | `deploy/streaming/fts.Dockerfile` | Receives car push, decodes, forwards to `/telemetry` |

## Step-by-step

### 1. Deploy the Vehicle Command Proxy

```bash
cd /Users/colinmburns/Documents/Projects/tesla-fleet
railway up --service vcp --detach \
    --dockerfile deploy/streaming/vcp.Dockerfile
```

In Railway dashboard for the `vcp` service:
- Add secret: `TESLA_PRIVATE_KEY` (contents of `secrets/tesla_private.pem`)
- Add a custom domain: `vcp.burnsbuilt.co` (or similar)
- Add CNAME in Cloudflare DNS pointing at the generated Railway hostname.

### 2. Deploy the Fleet Telemetry Server

```bash
railway up --service fts --detach \
    --dockerfile deploy/streaming/fts.Dockerfile
```

In Railway dashboard for the `fts` service:
- Add secret: `TESLA_PRIVATE_KEY` (same as above)
- Add env vars:
  - `SINK_URL=https://tesla.burnsbuilt.co/telemetry`
  - `SINK_TOKEN=` (any random string; reuse it as `TELEMETRY_TOKEN` on the main app)
- Add a custom domain: `fts.burnsbuilt.co`
- Cloudflare CNAME for it too

### 3. Lock the receiver

On the main `web` service, add env var:
- `TELEMETRY_TOKEN` = same string as `SINK_TOKEN`

The `/telemetry` handler validates this header so randoms on the internet
can't post fake events.

### 4. Register the telemetry config with Tesla

```bash
tesla-fleet telemetry register 5YJ3E1ETXRF901558 \
    --hostname fts.burnsbuilt.co \
    --proxy https://vcp.burnsbuilt.co
```

This routes the registration through the VCP, which signs it with the
partner key. Tesla validates and starts the car streaming on its next
ignition cycle. (Some cars only pick up new configs after a reboot —
hit P → unlock to force a refresh.)

### 5. Verify

After the next drive:
```bash
curl -s -u colin:goblin "https://tesla.burnsbuilt.co/api/events?type=telemetry_push&limit=5"
```
Should show 1Hz-cadence samples with real lat/lon and speed.

## Cost

- VCP: ~$2/mo (Railway shared-cpu, low traffic)
- FTS: ~$3/mo (a bit more memory for the Go runtime)
- Tesla Fleet API: **$0/mo** (streaming doesn't bill per-call)
- Total marginal cost: ~$5/mo, but we save the ~$15/mo we were spending on polls

## Fallback

If the streaming pipeline breaks, the existing FastAPI poller still runs
on adaptive cadence — it's the safety net. You can disable streaming
without losing data:
```bash
tesla-fleet telemetry unregister 5YJ3E1ETXRF901558 \
    --proxy https://vcp.burnsbuilt.co
```
