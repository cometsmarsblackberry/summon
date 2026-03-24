"""Helpers for deriving display flags for location records."""

from __future__ import annotations

import re
from typing import Optional


CIRCLE_FLAGS_BASE = "https://hatscripts.github.io/circle-flags/flags"

COUNTRY_ALIASES = {
    "australia": "AU",
    "azerbaijan": "AZ",
    "brazil": "BR",
    "canada": "CA",
    "chile": "CL",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "hong kong": "HK",
    "india": "IN",
    "israel": "IL",
    "japan": "JP",
    "kazakhstan": "KZ",
    "luxembourg": "LU",
    "mexico": "MX",
    "netherlands": "NL",
    "poland": "PL",
    "portugal": "PT",
    "south africa": "ZA",
    "south korea": "KR",
    "spain": "ES",
    "sweden": "SE",
    "north korea": "KP",
    "russia": "RU",
    "syria": "SY",
    "taiwan": "TW",
    "turkey": "TR",
    "united arab emirates": "AE",
    "uk": "GB",
    "united kingdom": "GB",
    "united states": "US",
    "uzbekistan": "UZ",
    "usa": "US",
    "vietnam": "VN",
}

def _normalize(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def country_code_from_name(country: Optional[str]) -> Optional[str]:
    normalized = _normalize(country)
    if not normalized:
        return None

    if normalized in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[normalized]

    try:
        import pycountry

        match = pycountry.countries.lookup(country)
        return match.alpha_2.upper()
    except Exception:
        return None


def normalize_subdivision(value: Optional[str]) -> Optional[str]:
    """Normalize a subdivision value.

    Accepts ISO 3166-2 codes (e.g. "US-CA") or plain names (e.g. "California").
    ISO codes are uppercased; plain names are title-cased.
    """
    if not value or not value.strip():
        return None

    trimmed = value.strip()

    # Check if it looks like an ISO 3166-2 code
    normalized = trimmed.upper().replace("_", "-")
    if re.fullmatch(r"[A-Z]{2}-[A-Z0-9]{1,8}", normalized):
        return normalized

    # Accept as a plain name (title-case it)
    return trimmed.title()


def build_location_flag(
    *,
    country: Optional[str],
    city: Optional[str] = None,
    name: Optional[str] = None,
    provider_region: Optional[str] = None,
    subdivision: Optional[str] = None,
) -> Optional[dict[str, str]]:
    """Return flag metadata for the frontend, or None if unavailable."""
    country_code = country_code_from_name(country)
    if not country_code:
        return None
    return {
        "kind": "image",
        "src": f"{CIRCLE_FLAGS_BASE}/{country_code.lower()}.svg",
        "alt": f"{country or country_code} flag",
    }
