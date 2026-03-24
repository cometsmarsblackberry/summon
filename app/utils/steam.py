"""Steam ID conversion utilities."""

def steamid64_to_steamid2(steamid64: str | int) -> str:
    """Convert SteamID64 to SteamID2 format (STEAM_0:X:Y)."""
    try:
        steamid64 = int(steamid64)
    except (ValueError, TypeError):
        return str(steamid64)

    # SteamID64 base for individual accounts
    base = 76561197960265728
    
    if steamid64 < base:
        return str(steamid64)
    
    relative_id = steamid64 - base
    y = relative_id // 2
    x = relative_id % 2
    
    return f"STEAM_0:{x}:{y}"
