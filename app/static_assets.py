"""Content-fingerprinted URLs and serving for browser-cacheable assets."""

from __future__ import annotations

import hashlib
import hmac
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


STATIC_ROOT = (Path(__file__).resolve().parent.parent / "static").resolve()

FINGERPRINTED_STATIC_CACHE_CONTROL = "public, max-age=31536000, immutable"
STABLE_STATIC_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"

# These files are update/bootstrap channels rather than frontend assets. Keep
# their stable URLs and force every request to reach the application.
MUTABLE_STATIC_ASSET_PATHS = frozenset(
    {
        "install-instant-host.sh",
        "tf2-agent",
    }
)

_FINGERPRINT_PATTERN = re.compile(
    r"^v-(?P<fingerprint>[0-9a-f]{64})/(?P<asset_path>.+)$"
)


def _normalise_asset_path(asset_path: str) -> tuple[str, Path]:
    """Return a safe POSIX asset path and its absolute filesystem path."""

    if not asset_path or "\\" in asset_path:
        raise ValueError("static asset path must be a non-empty POSIX path")

    relative = PurePosixPath(asset_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("static asset path must stay inside the static directory")

    logical_path = relative.as_posix()
    if logical_path in {"", "."}:
        raise ValueError("static asset path must name a file")

    full_path = (STATIC_ROOT / Path(*relative.parts)).resolve()
    if not full_path.is_relative_to(STATIC_ROOT):
        raise ValueError("static asset path must stay inside the static directory")

    return logical_path, full_path


@lru_cache(maxsize=512)
def _hash_file(path: str, modified_ns: int, size: int) -> str:
    """Hash a file, with its stat values making development changes visible."""

    del modified_ns, size
    digest = hashlib.sha256()
    with open(path, "rb") as asset:
        for chunk in iter(lambda: asset.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def static_asset_fingerprint(asset_path: str) -> str:
    """Return the SHA-256 fingerprint for an existing static asset."""

    logical_path, full_path = _normalise_asset_path(asset_path)
    if logical_path in MUTABLE_STATIC_ASSET_PATHS:
        raise ValueError(f"mutable static asset cannot be fingerprinted: {logical_path}")

    file_stat = full_path.stat()
    if not full_path.is_file():
        raise FileNotFoundError(full_path)

    return _hash_file(str(full_path), file_stat.st_mtime_ns, file_stat.st_size)


def static_asset_url(asset_path: str) -> str:
    """Build a URL whose path changes whenever the asset content changes.

    Generated assets such as Tailwind CSS do not exist in a clean source
    checkout. In that case, preserve the old stable URL so non-container test
    and development environments can still render pages; the request will
    revalidate and naturally return 404 until the asset is built.
    """

    logical_path, _ = _normalise_asset_path(asset_path)
    encoded_path = quote(logical_path, safe="/")

    try:
        fingerprint = static_asset_fingerprint(logical_path)
    except FileNotFoundError:
        return f"/static/{encoded_path}"

    return f"/static/v-{fingerprint}/{encoded_path}"


class FingerprintedStaticFiles(StaticFiles):
    """Serve valid fingerprint paths while retaining stable legacy paths."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        match = _FINGERPRINT_PATTERN.fullmatch(path)
        if match is None:
            return await super().get_response(path, scope)

        logical_path = match.group("asset_path")
        try:
            expected_fingerprint = static_asset_fingerprint(logical_path)
        except (FileNotFoundError, OSError, ValueError):
            raise HTTPException(status_code=404)

        if not hmac.compare_digest(
            match.group("fingerprint"),
            expected_fingerprint,
        ):
            raise HTTPException(status_code=404)

        response = await super().get_response(logical_path, scope)
        response.headers["Cache-Control"] = FINGERPRINTED_STATIC_CACHE_CONTROL
        return response
