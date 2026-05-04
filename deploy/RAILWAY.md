# Deploying to Railway

## One-time setup (you do, ~10 min)

1. **Create the project**
   - https://railway.app → New Project → Deploy from GitHub repo
   - Push this repo to GitHub first (or use `railway up` from CLI for direct deploy)
   - Railway auto-detects the `Dockerfile` and `railway.toml`

2. **Add a persistent volume**
   - Service → Settings → Volumes → New Volume
   - Name: `tesla_data`
   - Mount path: `/data`
   - Size: `1 GB` is plenty (SQLite for 17 days of full telemetry was 14 MB)

3. **Set environment variables** (Service → Variables → Raw Editor → paste)
   ```
   TESLA_CLIENT_ID=3ef90df9-d6ca-4a45-8e50-18612781733a
   TESLA_CLIENT_SECRET=ta-secret.4kJrwk_X7E-MU$os
   TESLA_REDIRECT_URI=https://tesla.burnsbuilt.co/callback
   TESLA_PUBLIC_HOSTNAME=tesla.burnsbuilt.co
   TESLA_AUDIENCE=https://fleet-api.prd.na.vn.cloud.tesla.com
   TESLA_REGION=na
   TESLA_DEFAULT_DRIVER=Colin
   TESLA_SCOPES=openid offline_access user_data vehicle_device_data vehicle_cmds vehicle_charging_cmds
   ```
   Plus the partner private key (paste contents of `secrets/tesla_private.pem`):
   ```
   TESLA_PRIVATE_KEY=-----BEGIN EC PRIVATE KEY-----
   ...
   -----END EC PRIVATE KEY-----
   ```

4. **Custom domain**
   - Service → Settings → Networking → Custom Domain
   - Enter `tesla.burnsbuilt.co`
   - Railway gives you a CNAME target like `something.up.railway.app`
   - In **Cloudflare DNS** for `burnsbuilt.co`:
     - Type `CNAME`, Name `tesla`, Target `<railway-given-target>`, Proxy status: **DNS only** (gray cloud)
     - (Proxied/orange cloud breaks Railway's TLS provisioning)
   - Wait 1–5 min; Railway issues a Let's Encrypt cert automatically

5. **Tesla developer portal** (https://developer.tesla.com)
   - Allowed Origin: `https://tesla.burnsbuilt.co`
   - Redirect URI: `https://tesla.burnsbuilt.co/callback`
   - Public Key URL: `https://tesla.burnsbuilt.co/.well-known/appspecific/com.tesla.3p.public-key.pem`
   - Click **Validate** so Tesla fetches the key

6. **Bootstrap data** (one-time, after first successful deploy)
   - From your Mac, with Railway CLI installed (`brew install railway` or `curl -fsSL https://railway.com/install.sh | sh`):
     ```bash
     railway link        # pick the project
     railway shell        # gets you a shell on the running container
     # Then inside the container:
     tesla-fleet-import /tmp/import --key-map "1038929084=Colin,4136030175=Carson,2851572947=Lindsey"
     ```
   - Or upload your `tesla_export/` folder to the volume via `railway run` or scp-style transfer.
   - Easiest: run the importer locally to produce `tesla.db`, then `railway run --detach 'cp tesla.db /data/tesla.db'` after copying the file in.

## Cost
- Hobby plan: **$5/mo** included usage; this app uses ~$2-3/mo (256 MB RAM, ~2% CPU avg, <1 GB volume).
- Effectively **$5/mo** until you exceed Hobby.

## After it's live
- Visit https://tesla.burnsbuilt.co → log in via OAuth callback
- Run `tesla-fleet telemetry register <VIN>` from the Railway shell to switch from polling to streaming (recommended once domain + cert + public-key URL all validate green)

## Rollback / debug
- `railway logs` — live tail
- `railway deploy --detach` — redeploy current commit
- Roll back via dashboard → Deployments → previous deployment → Redeploy
