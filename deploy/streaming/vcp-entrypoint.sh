#!/bin/sh
# Materialize partner private key + self-signed listener cert from env vars,
# then start the proxy on $PORT (Railway-assigned).
set -e

KEY_DIR=/etc/proxy
mkdir -p "$KEY_DIR"

if [ -z "$TESLA_PRIVATE_KEY" ]; then
  echo "TESLA_PRIVATE_KEY env var not set" >&2; exit 1
fi
printf '%s\n' "$TESLA_PRIVATE_KEY" > "$KEY_DIR/partner.pem"
chmod 600 "$KEY_DIR/partner.pem"

# Generate listener cert/key on first run. The proxy listens with TLS;
# we trust this cert internally (only our own backend hits the proxy).
if [ ! -f "$KEY_DIR/server.crt" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$KEY_DIR/server.key" -out "$KEY_DIR/server.crt" \
    -subj "/CN=vcp.internal" >/dev/null 2>&1
fi

exec tesla-http-proxy \
  -key-file "$KEY_DIR/partner.pem" \
  -cert "$KEY_DIR/server.crt" \
  -tls-key "$KEY_DIR/server.key" \
  -port "${PORT:-4443}" \
  -host "${PROXY_HOST:-0.0.0.0}"
