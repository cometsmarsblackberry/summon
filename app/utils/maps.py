"""Helpers for validating TF2 map identifiers."""

import re


_MAP_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def is_valid_map_name(name: str) -> bool:
    """Return True when the value is a safe TF2 map identifier."""
    candidate = (name or "").strip()
    return bool(_MAP_NAME_RE.fullmatch(candidate))
