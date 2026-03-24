# Summon

On-demand Team Fortress 2 server reservation system. Reserve temporary game servers across multiple cloud providers (Vultr, Gcore) through a web UI.

## Quick Start

```bash
cp .env.example .env   # configure API keys, Steam credentials, etc.
docker-compose up       # http://localhost:8000
```

## Features

- Steam OAuth login
- Multi-provider server provisioning (Vultr, Gcore)
- Real-time server status via heartbeats from a Go agent
- Auto-expiry and cleanup of unused servers
- hCaptcha integration
- SourceMod plugin for in-game management
- i18n and customizable branding

## Production Deploy

```bash
./deploy.sh [--bunny] [user@host]
```

Requires Python 3.12+, Docker, and Go 1.19+ (for the agent).
