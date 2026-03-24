"""Competitive config classification and grouping.

The authoritative list of available configs comes from the TF2 server image
(reported by the agent/plugin). The backend only classifies and groups config
identifiers for UI consumption.
"""

from __future__ import annotations

from dataclasses import dataclass


ALLOWED_PREFIXES = ("rgl_", "etf2l_", "fbtf_", "tfarena_", "ultitrio_", "ozfortress_", "cltf2_")
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
    # Fallback single-prefix entries for configs like etf2l_ultiduo
    ("etf2l_", "ETF2L", "Other"),
    ("fbtf_", "FBTF", "Other"),
    ("rgl_", "RGL", "Other"),
]

_BASE_STEMS = {prefix.rstrip("_") for prefix, _, _ in _FORMAT_MAP}


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


def group_for_ui(cfg_files: list[str]) -> dict[str, dict[str, list[dict]]]:
    """Return configs grouped by league -> format for API/UI consumption."""
    grouped: dict[str, dict[str, list[dict]]] = {}
    for cfg_file in filter_user_selectable(cfg_files):
        league, fmt = classify_config(cfg_file)
        league_bucket = grouped.setdefault(league, {})
        fmt_bucket = league_bucket.setdefault(fmt, [])
        fmt_bucket.append({"cfg_file": cfg_file, "name": cfg_file})
    return grouped
