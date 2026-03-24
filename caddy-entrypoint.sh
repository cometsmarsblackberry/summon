#!/bin/sh
set -e

# Site address Caddy listens on and obtains a TLS certificate for.
# Defaults to BASE_URL (direct access, no CDN). When behind a CDN, set
# ORIGIN_HOSTNAME to the origin domain (e.g. origin.example.com) so Caddy
# gets a cert via HTTP-01 without needing DNS API keys.
if [ -z "$ORIGIN_HOSTNAME" ]; then
    export ORIGIN_HOSTNAME="${BASE_URL}"
fi

exec "$@"
