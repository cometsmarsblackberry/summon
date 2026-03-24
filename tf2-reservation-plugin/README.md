# Summon SourceMod Plugin

A SourceMod plugin for in-game reservation management on TF2 servers.

## Features

### Player Commands
- `!who` - Show who reserved the server
- `!reservation` - Show reservation info and time remaining

### Owner Commands
- `!end` - End the reservation (30 second countdown)
- `!cancel` - Cancel pending end countdown

### Server Commands (RCON)
These are triggered by the Go agent:
- `sm_reservation_warning <minutes>` - Show time remaining warning
- `sm_reservation_ending` - Trigger end sequence

## Dependencies

- SourceMod 1.11+
- [ripext](https://github.com/ErikMinekus/sm-ripext) extension for HTTP requests

## Configuration

ConVars are set dynamically by the agent via RCON at server boot:

| ConVar | Description |
|--------|-------------|
| `sm_reserve_owner` | Steam ID of reservation owner (Steam3 format) |
| `sm_reserve_owner_name` | Display name of owner |
| `sm_reserve_number` | Reservation number |
| `sm_reserve_ends_at` | Unix timestamp when reservation ends |
| `sm_reserve_backend_url` | Backend API URL |
| `sm_reserve_api_key` | Internal API key |

## Installation

1. Compile `scripting/summon.sp` with spcomp
2. Place `summon.smx` in `addons/sourcemod/plugins/`
3. Plugin is automatically configured by the agent on boot

## Building

```bash
# Requires SourceMod compiler (spcomp)
cd scripting
spcomp summon.sp -o../plugins/summon.smx
```
