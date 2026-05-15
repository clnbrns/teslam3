#!/bin/sh
# Materialize TLS cert (Let's Encrypt-issued by Railway public domain) and
# the partner key, then start fleet-telemetry with the env-substituted config.
set -e

CERT_DIR=/etc/fts
mkdir -p "$CERT_DIR"

# When deployed behind Railway, $RAILWAY_PUBLIC_DOMAIN is set automatically;
# we use Railway's TLS termination so this internal listener can be plain.
# For local/dev we ship a self-signed cert.
if [ ! -f "$CERT_DIR/server.crt" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" \
    -subj "/CN=${RAILWAY_PUBLIC_DOMAIN:-fts.local}" >/dev/null 2>&1
fi

# Substitute env vars (SINK_URL, SINK_TOKEN) into the config template.
envsubst < /etc/fts/config.json > /etc/fts/config.runtime.json

exec fleet-telemetry -config /etc/fts/config.runtime.json
