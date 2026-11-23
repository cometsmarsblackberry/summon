"""Competitive config classification and grouping.

The authoritative list of available configs comes from the TF2 server image
(reported by the agent/plugin). The backend only classifies and groups config
identifiers for UI consumption.
"""

from __future__ import annotations

from dataclasses import dataclass


ALLOWED_PREFIXES = (
    "rgl_", "etf2l_", "fbtf_", "tfarena_", "ultitrio_", "ozfortress_", "cltf2_", "afc_", "af_",
    "1v1mge", "2v2mge", "kotf",
)
_SKIP_SUFFIXES = ("_base", "_custom", "_common")

_FORMAT_MAP: list[tuple[str, str, str]] = [
    ("rgl_6s_", "RGL", "6v6"),
    ("rgl_7s_", "RGL", "Prolander"),
    ("rgl_HL_", "RGL", "Highlander"),
    ("rgl_mm_", "RGL", "No Restriction"),
    ("rgl_ud_", "RGL", "Ultiduo"),
    ("rgl_pt_", "RGL", "Pass Time"),
    ("etf2l_6v6_", "ETF2L", "6v6"),
    ("etf2l_9v9_", "ETF2L", "Highlander"),
    ("fbtf_6v6_", "FBTF", "6v6"),
    ("tfarena_6v6_", "TFArena", "6v6"),
    # Fallback for other tfarena configs
    ("tfarena_", "TFArena", "Other"),
    ("ultitrio_", "Ultitrio", "Other"),
    ("ozfortress_4v4_", "ozfortress", "4v4"),
    ("ozfortress_6v6_", "ozfortress", "6v6"),
    ("ozfortress_hl_", "ozfortress", "Highlander"),
    ("ozfortress_", "ozfortress", "Other"),
    ("cltf2_4s_", "CLTF2", "4v4"),
    ("cltf2_", "CLTF2", "Other"),
    # AsiaFortress names its 6s configs afc_* and its Highlander configs af_*
    ("afc_6s_", "AsiaFortress", "6v6"),
    ("afc_", "AsiaFortress", "Other"),
    ("af_HL_", "AsiaFortress", "Highlander"),
    ("af_", "AsiaFortress", "Other"),
    # The MGE configs are named after their format, without a league prefix.
    ("1v1mge", "MGE", "1v1"),
    ("2v2mge", "MGE", "2v2"),
    # The King of the Flag config has no league prefix either. It allows two
    # soldiers per team.
    ("kotf", "KOTF", "2v2"),
    # Fallback single-prefix entries for configs like etf2l_ultiduo
    ("etf2l_", "ETF2L", "Other"),
    ("fbtf_", "FBTF", "Other"),
    ("rgl_", "RGL", "Other"),
]

# Stems like "etf2l" and "rgl" are league base configs. Entries without a
# trailing "_" are whole config names, not prefixes.
_BASE_STEMS = {prefix.rstrip("_") for prefix, _, _ in _FORMAT_MAP if prefix.endswith("_")}

# Main leagues are listed first in the UI; the rest follow alphabetically.
# The in-game config menu mirrors this order in
# tf2-reservation-plugin/scripting/summon.sp.
LEAGUE_PRIORITY = ("ETF2L", "RGL", "ozfortress", "AsiaFortress", "FBTF")


# "Auto" is not a cfg file. The plugin picks the config itself: the league from
# the server's region, the game mode from the map, and 6v6 or Highlander from
# how many players join each team. The plugin is told the league as
# "auto_<league>", where <league> is a key of AUTO_LEAGUES.
AUTO_CONFIG = "auto"
AUTO_LEAGUES = {
    "etf2l": "ETF2L",
    "rgl": "RGL",
    "ozfortress": "ozfortress",
}
_RGL_COUNTRIES = {"United States", "Canada", "Mexico"}
_OZFORTRESS_COUNTRIES = {"Australia", "New Zealand"}


def auto_config_league(country: str | None, continent: str | None) -> str:
    """Return the AUTO_LEAGUES key of the main league where a server is.

    The country comes first, since Honolulu's continent is Oceania. Players
    in Europe and next to it, including Turkey and Central Asia, play ETF2L,
    which is also the default for regions with no main league of their own.
    """
    if country in _RGL_COUNTRIES or continent in ("North America", "South America"):
        return "rgl"
    if country in _OZFORTRESS_COUNTRIES or continent == "Australia":
        return "ozfortress"
    return "etf2l"


def auto_config_command(league: str | None) -> str:
    """Return the sm_config argument that turns Auto on with a league."""
    return f"{AUTO_CONFIG}_{league if league in AUTO_LEAGUES else 'etf2l'}"


def startup_config_file(reservation) -> str:
    """Return the sm_config argument a reservation's server runs at startup."""
    if getattr(reservation, "auto_config", False):
        return auto_config_command(getattr(reservation, "auto_config_league", None))
    return reservation.config_file or ""


async def reservation_auto_config_league(reservation, db) -> str:
    """Return the league Auto uses on a reservation's server.

    Reservations made before Auto have none stored, so it comes from their
    location and is stored. The caller commits.
    """
    league = getattr(reservation, "auto_config_league", None)
    if league in AUTO_LEAGUES:
        return league

    from app.models.instance import EnabledLocation

    location = await db.get(EnabledLocation, reservation.location)
    league = auto_config_league(
        location.country if location else None,
        location.continent if location else None,
    )
    reservation.auto_config_league = league
    return league


@dataclass(frozen=True)
class CompetitiveConfig:
    cfg_file: str
    league: str
    format: str
    name: str


def classify_config(cfg_file: str) -> tuple[str, str]:
    for prefix, league, fmt in _FORMAT_MAP:
        if cfg_file.startswith(prefix):
            return league, fmt
    return "Other", "Other"


def filter_user_selectable(cfg_files: list[str]) -> list[str]:
    """Filter raw cfg identifiers to those we want to expose in the UI."""
    out: list[str] = []
    for stem in cfg_files:
        if not stem.startswith(ALLOWED_PREFIXES):
            continue
        if stem.endswith(_SKIP_SUFFIXES):
            continue
        if stem in _BASE_STEMS:
            continue
        if stem == "summon_reset":
            # Shown as a dedicated Reset button
            continue
        out.append(stem)
    return sorted(set(out))


def _league_sort_key(league: str) -> tuple[int, str]:
    if league in LEAGUE_PRIORITY:
        return LEAGUE_PRIORITY.index(league), ""
    return len(LEAGUE_PRIORITY), league.casefold()


def group_for_ui(cfg_files: list[str]) -> dict[str, dict[str, list[dict]]]:
    """Return configs grouped by league -> format for API/UI consumption.

    League keys are ordered for display (see LEAGUE_PRIORITY).
    """
    grouped: dict[str, dict[str, list[dict]]] = {}
    for cfg_file in filter_user_selectable(cfg_files):
        league, fmt = classify_config(cfg_file)
        league_bucket = grouped.setdefault(league, {})
        fmt_bucket = league_bucket.setdefault(fmt, [])
        fmt_bucket.append({"cfg_file": cfg_file, "name": cfg_file})
    return {league: grouped[league] for league in sorted(grouped, key=_league_sort_key)}
