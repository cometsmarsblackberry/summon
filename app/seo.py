"""Public URL rules for search engines: localized paths and indexable pages.

English pages live at unprefixed paths (``/about``) and negotiate the
language from the cookie or Accept-Language header. Every other
supported language also has a fixed-language copy of each indexable page
under a path prefix (``/fi/about``), so search engines can index it and
``hreflang`` alternates can point at it.
"""

from pathlib import Path

from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.i18n import DEFAULT_LOCALE, SUPPORTED_LOCALES


settings = get_settings()

LOCALIZED_LOCALES = tuple(
    locale for locale in SUPPORTED_LOCALES if locale != DEFAULT_LOCALE
)

# Pages that search engines may index, in sitemap order. Everything else
# (per-user pages, reservation pages, bans, feedback, errors) is noindex.
_INDEXABLE_PATHS = ["/", "/about", "/stats", "/ping", "/maps"]
# Same check as the /privacy route registration in app/routers/pages.py.
if Path("templates/privacy.html").exists():
    _INDEXABLE_PATHS.append("/privacy")
INDEXABLE_PATHS = tuple(_INDEXABLE_PATHS)
_INDEXABLE_PATH_SET = frozenset(INDEXABLE_PATHS)


def is_indexable_path(path: str) -> bool:
    return path in _INDEXABLE_PATH_SET


def split_locale_prefix(path: str) -> tuple[str | None, str]:
    """Split a localized path into its locale and unprefixed path.

    ``/fi/about`` -> ``("fi", "/about")``, ``/fi/`` -> ``("fi", "/")``.
    Paths without a non-default locale prefix come back as ``(None, path)``.
    """
    segment, _, remainder = path[1:].partition("/")
    if segment in LOCALIZED_LOCALES:
        return segment, "/" + remainder
    return None, path


def localized_path(path: str, locale: str | None) -> str:
    """Return the URL path of ``path`` in ``locale``.

    Only indexable pages have localized copies; English and every other
    path come back unchanged. A query string or fragment is kept.
    """
    split_at = min(
        (index for index in (path.find("?"), path.find("#")) if index >= 0),
        default=len(path),
    )
    base, suffix = path[:split_at], path[split_at:]
    if locale not in LOCALIZED_LOCALES or not is_indexable_path(base):
        return path
    return (f"/{locale}/" if base == "/" else f"/{locale}{base}") + suffix


def absolute_url(path: str) -> str:
    return settings.base_url.rstrip("/") + path


def alternate_links(path: str) -> list[tuple[str, str]]:
    """``(hreflang, absolute URL)`` pairs for every language of a page."""
    links = [
        (locale, absolute_url(localized_path(path, locale)))
        for locale in SUPPORTED_LOCALES
    ]
    links.append(("x-default", absolute_url(path)))
    return links


def _redirect_target(path: str, raw_path: str) -> str | None:
    """Return the normalised path for a GET/HEAD request, if it differs."""
    segment, _, remainder = path[1:].partition("/")

    # The localized home page is /fi/, never /fi.
    if segment in LOCALIZED_LOCALES and path == f"/{segment}":
        return path + "/"

    # English has no prefix, so /en/... would duplicate the real pages.
    if segment == DEFAULT_LOCALE:
        target = "/" + remainder.strip("/")
        return target if is_indexable_path(target) else None

    if path != "/" and path.endswith("/"):
        if segment in LOCALIZED_LOCALES and remainder == "":
            return None
        # Build from the raw path so percent-encoding survives, and keep a
        # single leading slash: "//host" or "/\host" would leave the site.
        return "/" + raw_path.strip("/").lstrip("/\\")

    return None


class PublicUrlMiddleware:
    """Normalise public URLs and route localized paths.

    Trailing slashes are removed with a relative 301 redirect. Starlette's
    own ``redirect_slashes`` builds an absolute URL from the Host header,
    which can contain a proxy origin hostname instead of the public hostname.

    For ``/<locale>/<indexable page>`` the prefix is stripped before routing
    and the locale is stored as ``request.state.url_locale``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if scope["method"] in ("GET", "HEAD"):
            raw_path = (scope.get("raw_path") or path.encode()).decode("latin-1")
            target = _redirect_target(path, raw_path)
            if target is not None:
                query = scope.get("query_string", b"")
                if query:
                    target += "?" + query.decode("latin-1")
                response = RedirectResponse(target, status_code=301)
                await response(scope, receive, send)
                return

        locale, remainder = split_locale_prefix(path)
        if locale is not None and is_indexable_path(remainder):
            scope = dict(scope)
            scope["path"] = remainder
            scope["raw_path"] = remainder.encode()
            scope["state"] = {**scope.get("state", {}), "url_locale": locale}

        await self.app(scope, receive, send)
