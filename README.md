# Summon

On-demand Team Fortress 2 server reservations. Players sign in with Steam,
pick a location, and get a private TF2 server for the length of their
reservation. Servers run on cloud VMs (Vultr, Gcore, Onidel) or on
operator-owned Instant hosts.

## Features

- Steam login, optional hCaptcha, and an allowlist-only beta mode
- Instant hosts with multiple server slots are used first, and cloud VMs take
  the overflow
- Live server status, player list, and map, config, and command controls on
  the reservation page
- Automatic expiry and cleanup of servers
- Competitive configs, logs.tf and demos.tf uploads, and optional S3 log storage
- Translations and customizable branding

## Repository layout

| Path | Contents |
| --- | --- |
| `app/`, `templates/`, `static/`, `locales/` | FastAPI web app: reservations, scheduling, cloud providers, admin panel |
| `agent/` | Go agent that runs on each cloud VM or Instant host, starts the TF2 container, and sends heartbeats |
| `tf2-reservation-plugin/` | SourceMod plugins for in-game reservation management ([README](tf2-reservation-plugin/README.md)) |

The TF2 server image is built in a separate repository,
[tf2-summon](https://github.com/cometsmarsblackberry/tf2-summon).

## Development

Requires Docker. To run the tests, you also need Python 3.12 and Go.

```bash
cp .env.example .env   # API keys, Steam, admin Steam IDs, etc.
docker compose up      # http://localhost:8000
```

For UI work without Steam or provider credentials, run `./scripts/preview.sh`.
It starts a disposable instance with fixture data that never creates real
servers. Open <http://127.0.0.1:8000/__dev/login> to sign in as a test user,
or `/__dev/active-reservation` for a running reservation backed by a fake
agent. Rebuild the preview after adding new Tailwind classes.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
scripts/test.sh        # Python tests, then Go agent tests
```

Templates should reference frontend assets with
`{{ static_asset('path/to/asset') }}`, which gives each one a cache-busting
URL.

## Deployment

CI publishes `ghcr.io/cometsmarsblackberry/summon` on every push to `main`
(tagged `latest` and `main`) and on every `v*.*.*` tag (semantic-version
tags). Every build is also tagged `sha-<commit>`.

```bash
cp .env.example .env
mkdir -p data/logs
sudo chown -R 65532:65532 data
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

The production stack puts Caddy in front of the app. Caddy obtains a TLS
certificate for `BASE_URL`, or for `ORIGIN_HOSTNAME` when the site is behind a
CDN. Set `SUMMON_IMAGE` to pin a release.

Instant hosts are added from the admin panel's Instant Hosts tab, which
generates a one-line install command for the host. Scheduling on them stays
off until `INSTANT_HOSTS_ENABLED` is turned on there or in `.env`.

## License

[GNU AGPL-3.0](LICENSE). The image also builds the agent, served at
`/static/tf2-agent`. The licenses of the agent's Go dependencies are served at
`/static/tf2-agent-licenses.txt`.
