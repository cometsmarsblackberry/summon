# Summon TF2 SourceMod Plugins

SourceMod plugins used by Summon reservation servers:

- `summon.sp` manages the active reservation, owner permissions, competitive
  configs, expiry, and backend reporting.
- `mapdownloader.sp` changes maps and downloads missing BSPs from a FastDL
  server. It can run with Summon or as a standalone admin utility.

These sources are deployed through the
[`tf2-summon`](https://github.com/cometsmarsblackberry/tf2-summon) server
image. Operators do not install the plugins or their dependencies separately.

## Features

- Shows the reservation owner, number, and remaining time in game.
- Warns players as the reservation approaches expiry and removes them when it
  ends.
- Lets the reservation owner change maps, load approved competitive configs,
  restart the match, and end the reservation.
- Gives the owner restricted access to an operator-maintained allowlist of
  server commands without granting unrestricted RCON access.
- Reports player count, SteamID64, connection time, and ping to the Summon
  backend every 10 seconds and when players join or leave.
- Reports logs.tf and demos.tf upload links when the corresponding plugins are
  installed.
- Downloads a missing map before changing to it when the companion map
  downloader is installed.

## Commands

Chat triggers use SourceMod's usual `!command` syntax. The equivalent console
command begins with `sm_`.

### All players

| Chat command | Description |
| --- | --- |
| `!reservation`, `!res`, `!who` | Show the reservation number, owner, and time remaining. |

### Reservation owner

| Chat command | Description |
| --- | --- |
| `!admin` | Open the Summon menu for reservation information, maps, configs, restarts, and ending or cancelling the reservation. |
| `!end` | Start a 10-second countdown, notify the backend, and then remove all players. |
| `!cancel` | Cancel an active `!end` countdown. |
| `!changemap <map>`, `!map <map>` | Change to a local map or download a missing map through `mapdownloader.smx`. |
| `!config [name]`, `!cfg [name]` | Load an approved competitive config. With no name, open the league/config menu. |
| `!restart` | Open a menu to restart the tournament, game, or round. |
| `!command <command> [arguments...]`, `!cmd ...`, `!rcon ...` | Run a command from the reservation-owner allowlist. |

`!admin`, `!who`, `!map`, and `!rcon` overlap standard SourceMod commands.
Summon listens for these commands without registering duplicate callbacks. A
caller who has access to the corresponding SourceMod command retains its stock
behavior; otherwise, `!who` shows reservation information and the other aliases
are routed through Summon's reservation-owner checks. The aliases therefore
require the standard SourceMod plugins that register them. Use `!reservation`,
`!changemap`, or `!command`/`!cmd` as the collision-free alternatives.

If another plugin has already registered `sm_command` or `sm_cmd`, Summon leaves
that entry point to the other plugin and the remaining command aliases continue
to work.

Competitive config names must match an existing `cfg/<name>.cfg` file and
start with one of these prefixes:

```text
rgl_  etf2l_  fbtf_  tfarena_  ultitrio_  ozfortress_  cltf2_
```

The special `summon_reset` config resets the server to its defaults. Loading an
RGL config also enables Summon's built-in RGL plugin set; loading
`summon_reset` disables it again.

### Administrators and server console

| Console command | Description |
| --- | --- |
| `sm_summon_reload_owner_commands` | Reload the owner-command allowlist. Requires the SourceMod config admin flag. |
| `sm_config <name>` | Load an approved competitive config from the server console. |
| `sm_reservation_warning <minutes>` | Broadcast a remaining-time warning. Intended for the Summon agent. |
| `sm_reservation_ending` | Broadcast the final message and remove players after five seconds. Intended for the Summon agent. |
| `sm_summon_owner_command <actor_steamid64> <command> [arguments...]` | Run an allowlisted owner command for the Summon web console. Intended for the backend/agent integration. |

The plugin also performs its own expiry countdown from `sm_reserve_ends_at`, so
agent-issued warning commands are not required for expiry enforcement.

## Configuration

The Summon agent sets these protected ConVars through local RCON during server
boot:

| ConVar | Description |
| --- | --- |
| `sm_reserve_owner` | Reservation owner's 17-digit SteamID64. |
| `sm_reserve_owner_name` | Reservation owner's display name. |
| `sm_reserve_number` | Numeric reservation identifier. |
| `sm_reserve_ends_at` | Reservation end time as a Unix timestamp in seconds. |
| `sm_reserve_backend_url` | Summon backend base URL, without a trailing slash. |
| `sm_reserve_api_key` | Internal API key sent as the `X-API-Key` header. |

For local plugin testing, set the same ConVars through RCON. The plugin does
not load the provided, commented `configs/summon.cfg` automatically; the
current ConVar names are listed above.

### Owner-command allowlist

`configs/summon_owner_commands.cfg` is the policy source used by the web
application. The `tf2-summon` image carries the same file at
`sourcemod/configs/summon_owner_commands.cfg` and installs it as
`addons/sourcemod/configs/summon_owner_commands.cfg`. Keep those copies in sync
when changing the policy.

Its `SummonOwnerCommands` sections are exact command or ConVar names:

```text
"SummonOwnerCommands"
{
    "mp_timelimit"
    {
    }
}
```

The supplied file contains the default gameplay-oriented allowlist. Commands
provided by optional image plugins remain safe to list but are reported as
unavailable while their plugin is not loaded. The web and in-game checks must
agree before a command can run.

Owner-command execution has the following safeguards:

- It is available only while the reservation is active.
- Command names must exactly match the allowlist; an unreadable, invalid,
  duplicate, or empty allowlist disables the feature.
- Semicolons, line breaks, control characters, and command lines of 512 bytes
  or more are rejected.
- `changelevel` requires exactly one map name containing only letters, numbers,
  or underscores.
- `mp_tournament_whitelist` may only read the current value, clear it, or name
  one existing `cfg/*.txt` file without path traversal.
- A shared one-second cooldown applies between owner commands, including
  commands sent through the web console.
- Every dispatched command and policy rejection is written to the SourceMod
  log with the reservation number and actor SteamID64.

After editing the file, run `sm_summon_reload_owner_commands` or reload the
plugin. Do not allowlist command-forwarding or config-execution commands unless
their transitive access is intentional.

### Map downloader

The server image maps its `SM_MAP_DOWNLOAD_BASE` environment variable to the
plugin's protected `sm_map_download_base` ConVar:

| Image variable | Default | Description |
| --- | --- | --- |
| `SM_MAP_DOWNLOAD_BASE` | `https://fastdl.serveme.tf/maps` | Base URL containing uncompressed `<map>.bsp` files. A trailing slash is optional. |

When Summon is loaded, it owns the player-facing `!changemap` permission check
and the fallback behavior for `!map`, then forwards approved requests to the
downloader. In standalone mode, the downloader follows SourceMod's
`ADMFLAG_CHANGEMAP` access check. Server-console and RCON `changelevel` requests,
including allowlisted owner commands, can also download missing maps.

Downloads time out after five minutes, reject non-200 responses and files
smaller than 1 KiB, and are saved under the server's `maps/` directory before
the level changes.

## Backend integration

When the backend URL, API key, and reservation number are set, `summon.smx`
sends authenticated JSON requests to:

| Endpoint | Purpose |
| --- | --- |
| `POST /internal/reservations/<number>/players` | Current human-player snapshot. |
| `POST /internal/reservations/<number>/end` | Owner-confirmed early end. |
| `POST /internal/reservations/<number>/uploads` | logs.tf or demos.tf upload metadata. |

Upload reporting listens for the optional `LogUploaded` and `DemoUploaded`
forwards. URLs beginning with `http://` are normalized to HTTPS before they are
sent.

## Deployment

Summon defaults to
`ghcr.io/cometsmarsblackberry/tf2-summon/i386:nightly`. That image includes
SourceMod, REST in Pawn (`ripext`), the compiled Summon and Map Downloader
plugins, the owner-command allowlist, and the supported competitive configs.
See the `tf2-summon` repository for image configuration, architecture variants,
manual container launches, builds, and contract tests.

Plugin source changes in this repository do not update the server image by
themselves. A release must also update the compiled artifacts in
`tf2-summon/plugins/`, mirror any allowlist changes into
`tf2-summon/sourcemod/configs/`, update the pinned checksums in its validation
scripts, and rebuild the image. Its contract test verifies that both plugins
load and that the allowlist can be reloaded.

For source development, `summon.sp` requires the `ripext`, `logstf`, and
`demostf` includes. The integrated Map Downloader build also requires the
consumer-supplied `summon.inc`; without it, `mapdownloader.sp` compiles in
standalone mode and does not receive Summon's owner-approved map-change
forward.
