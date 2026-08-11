# Summon

On-demand Team Fortress 2 server reservation system. Reserve temporary game servers across multiple cloud providers or operator-owned Instant hosts through a web UI.

## Quick Start

```bash
cp .env.example .env   # configure API keys, Steam credentials, etc.
docker compose up       # http://localhost:8000
```

## Local UI Preview

For UI development without Steam credentials, cloud provider keys, or a
connected game-server agent, start the disposable preview environment:

```bash
./scripts/preview.sh
```

Then open <http://127.0.0.1:8000/__dev/login>. The preview creates a local test
user and deterministic location, map, and competitive-config fixtures before
redirecting to the home page. Its database lives in an in-memory container
filesystem and is discarded when the script stops. It is intended for UI work
and does not provision real game servers.

To test the running-server controls, open
<http://127.0.0.1:8000/__dev/active-reservation>. This creates an active local
reservation backed by a harmless fake agent, so menus such as map and config
changes can be exercised without sending commands to a game server.

Application, template, locale, and tracked static-file changes are mounted into
the preview container. Rebuild it after introducing new Tailwind utility
classes so the generated stylesheet includes them. Set `SUMMON_PREVIEW_PORT`
to use a port other than 8000.

The `app.preview:app` entrypoint refuses to start unless `SUMMON_PREVIEW=1` is
set. Production continues to use `app.main:app` and does not expose the local
login route.

## Features

- Steam OAuth login
- Multi-provider server provisioning (Vultr, Gcore)
- Multi-slot operator-owned hosts with Instant-first, cloud-overflow scheduling
- Real-time server status via heartbeats from a Go agent
- Auto-expiry and cleanup of unused servers
- hCaptcha integration
- SourceMod plugin for in-game management
- i18n and customizable branding

## Production

```bash
cp .env.example .env
mkdir -p data/logs
sudo chown -R 65532:65532 data
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

## Container Images

GitHub Actions builds and publishes the Summon application image to GitHub
Container Registry on every push to `main` and every `v*.*.*` tag:

- `ghcr.io/cometsmarsblackberry/summon`

The default branch is tagged as `latest` and `main`. Version tags produce
semantic-version tags, and every published build also receives a `sha-<commit>`
tag. Pull requests build the images for validation without publishing them.

The production Compose file uses the official `caddy:2.11.4-alpine` image. Set
`CADDY_IMAGE` only when the deployment needs a compatible Caddy build with
additional modules.
