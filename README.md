# Summon

On-demand Team Fortress 2 server reservation system. Reserve temporary game servers across multiple cloud providers (Vultr, Gcore) through a web UI.

## Quick Start

```bash
cp .env.example .env   # configure API keys, Steam credentials, etc.
docker compose up       # http://localhost:8000
```

## Features

- Steam OAuth login
- Multi-provider server provisioning (Vultr, Gcore)
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
