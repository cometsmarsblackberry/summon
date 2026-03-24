"""Internationalization support."""

import contextvars
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import Request


logger = logging.getLogger(__name__)

SUPPORTED_LOCALES = ("en", "es", "fil", "ms", "pt", "fi", "sv", "vi", "ja", "ko", "th")
DEFAULT_LOCALE = "en"
LOCALE_COOKIE = "lang"

# Human-readable names for the language switcher
# English first, then Latin-script languages alphabetically by native name,
# then CJK languages by Unicode codepoint.
LOCALE_NAMES = {
    "en": "English",
    "es": "Español",
    "fil": "Filipino",
    "ms": "Bahasa Melayu",
    "pt": "Português",
    "fi": "Suomi",
    "sv": "Svenska",
    "vi": "Tiếng Việt",
    "ja": "日本語",
    "ko": "한국어",
    "th": "ภาษาไทย",
}

# Loaded translations: {"en": {"key": "value", ...}, ...}
_translations: dict[str, dict[str, str]] = {}

# Context variable for the current request locale (set by middleware)
_current_locale: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_locale", default=DEFAULT_LOCALE
)


def _load_translations() -> None:
    """Load all locale JSON files from the locales/ directory.

    After loading the base translations, merge any instance-specific
    overrides from locales/local/*.json (gitignored).  This allows
    site operators to add keys (e.g. a privacy policy) that stay
    out of the public repository.
    """
    locales_dir = Path(__file__).resolve().parent.parent / "locales"
    local_dir = locales_dir / "local"
    for locale in SUPPORTED_LOCALES:
        path = locales_dir / f"{locale}.json"
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    _translations[locale] = _flatten(json.load(f))
            except Exception:
                logger.warning("Failed to load locale file %s", path, exc_info=True)
                _translations[locale] = {}
        else:
            _translations[locale] = {}

        # Merge instance-specific overrides
        local_path = local_dir / f"{locale}.json"
        if local_path.exists():
            try:
                with open(local_path, encoding="utf-8") as f:
                    _translations[locale].update(_flatten(json.load(f)))
            except Exception:
                logger.warning("Failed to load local locale file %s", local_path, exc_info=True)


def _flatten(data: dict, prefix: str = "") -> dict[str, str]:
    """Flatten nested dict into dot-separated keys.

    {"nav": {"faq": "FAQ"}} -> {"nav.faq": "FAQ"}
    """
    result: dict[str, str] = {}
    for key, value in data.items():
        full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict):
            result.update(_flatten(value, full_key))
        else:
            result[full_key] = str(value)
    return result


def get_locale(request: Request) -> str:
    """Determine the locale for a request.

    Priority:
    1. Cookie (lang=xx)
    2. Accept-Language header
    3. Default (en)
    """
    # 1. Cookie
    cookie_lang = request.cookies.get(LOCALE_COOKIE)
    if cookie_lang and cookie_lang in SUPPORTED_LOCALES:
        return cookie_lang

    # 2. Accept-Language header
    accept = request.headers.get("accept-language", "")
    for part in accept.split(","):
        # Parse "en-US;q=0.9" -> "en"
        lang = part.split(";")[0].strip().lower()
        # Try exact match first (e.g. "pt")
        if lang in SUPPORTED_LOCALES:
            return lang
        # Try prefix match (e.g. "pt-BR" -> "pt")
        prefix = lang.split("-")[0]
        if prefix in SUPPORTED_LOCALES:
            return prefix

    # 3. Default
    return DEFAULT_LOCALE


def translate(key: str, locale: str = DEFAULT_LOCALE, **kwargs: object) -> str:
    """Look up a translation key for the given locale.

    Falls back to English if the key is missing in the requested locale.
    Falls back to the key itself if missing everywhere.

    Supports Python format-style interpolation:
        translate("home.locations_count", locale, count=29)
        # en.json: "Choose from {count} locations"
        # -> "Choose from 29 locations"
    """
    if not _translations:
        _load_translations()

    # Try requested locale
    translations = _translations.get(locale, {})
    value = translations.get(key)

    # Fall back to English
    if not value and locale != DEFAULT_LOCALE:
        value = _translations.get(DEFAULT_LOCALE, {}).get(key)

    # Fall back to key itself
    if not value:
        return key

    # Always inject branding vars (site_name etc.) so {site_name} resolves
    # in any locale string without explicit kwargs at every call site.
    from app.config import get_settings
    branding = {"site_name": get_settings().site_name}
    merged = {**branding, **kwargs}
    try:
        return value.format(**merged)
    except (KeyError, IndexError):
        return value


def make_translate_func(request: Request):
    """Create a request-scoped translate function for use in Jinja2 templates.

    Usage in templates: {{ _('nav.faq') }}
    """
    locale = getattr(request.state, "locale", DEFAULT_LOCALE)

    def _(key: str, **kwargs: object) -> str:
        return translate(key, locale, **kwargs)

    return _


def set_current_locale(locale: str) -> None:
    """Set the locale for the current request context (called by middleware)."""
    _current_locale.set(locale)


def current_locale() -> str:
    """Get the locale for the current request context."""
    return _current_locale.get()


def t(key: str, **kwargs: object) -> str:
    """Translate using the current request's locale (from context variable).

    Use this in Python code (routers, services) where you don't have
    a request object handy:

        from app.i18n import t
        raise HTTPException(400, detail=t("errors.banned"))
    """
    return translate(key, current_locale(), **kwargs)


def get_translations_json(locale: str) -> dict[str, str]:
    """Get flat translations dict for a locale (for passing to JavaScript)."""
    if not _translations:
        _load_translations()
    return _translations.get(locale, _translations.get(DEFAULT_LOCALE, {}))


# Load translations at import time
_load_translations()
