"""Helpers for validating externally reported upload links."""

from urllib.parse import urlparse


_ALLOWED_UPLOAD_HOSTS = {
    "log": {"logs.tf", "www.logs.tf"},
    "demo": {"demos.tf", "www.demos.tf"},
}


def is_allowed_upload_url(url: str, upload_type: str) -> bool:
    """Return True when the URL uses HTTPS and matches the expected host."""
    parsed = urlparse((url or "").strip())
    allowed_hosts = _ALLOWED_UPLOAD_HOSTS.get(upload_type, set())
    return parsed.scheme == "https" and parsed.hostname in allowed_hosts
