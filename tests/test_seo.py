import asyncio
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from xml.etree import ElementTree

from fastapi.testclient import TestClient

from app import database
from app.config import get_settings
from app.i18n import SUPPORTED_LOCALES
from app.main import app
from app.routers import pages
from app.seo import (
    INDEXABLE_PATHS,
    _redirect_target,
    alternate_links,
    localized_path,
    split_locale_prefix,
)


BASE = get_settings().base_url.rstrip("/")
NOINDEX = '<meta name="robots" content="noindex">'
SITEMAP_NS = {
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "xhtml": "http://www.w3.org/1999/xhtml",
}


def setUpModule():
    async def create():
        await database.create_tables()
        # The TestClient runs requests on its own event loop.
        await database.engine.dispose()

    asyncio.run(create())


def canonical(html: str) -> str | None:
    match = re.search(r'<link rel="canonical" href="([^"]*)">', html)
    return match.group(1) if match else None


def hreflangs(html: str) -> dict[str, str]:
    return dict(re.findall(r'<link rel="alternate" hreflang="([^"]+)" href="([^"]+)">', html))


class UrlHelperTests(unittest.TestCase):
    def test_localized_path(self):
        self.assertEqual("/fi/about", localized_path("/about", "fi"))
        self.assertEqual("/fi/", localized_path("/", "fi"))
        self.assertEqual("/ja/stats?tab=ping", localized_path("/stats?tab=ping", "ja"))
        self.assertEqual("/about", localized_path("/about", "en"))
        self.assertEqual("/about", localized_path("/about", None))
        self.assertEqual("/bans", localized_path("/bans", "fi"))
        self.assertEqual("/reservations/5", localized_path("/reservations/5", "fi"))

    def test_split_locale_prefix(self):
        self.assertEqual(("fi", "/about"), split_locale_prefix("/fi/about"))
        self.assertEqual(("fi", "/"), split_locale_prefix("/fi/"))
        self.assertEqual((None, "/en/about"), split_locale_prefix("/en/about"))
        self.assertEqual((None, "/about"), split_locale_prefix("/about"))
        self.assertEqual((None, "/finland"), split_locale_prefix("/finland"))

    def test_alternate_links_cover_every_locale_and_x_default(self):
        links = dict(alternate_links("/about"))
        self.assertEqual(set(SUPPORTED_LOCALES) | {"x-default"}, set(links))
        self.assertEqual(f"{BASE}/about", links["en"])
        self.assertEqual(f"{BASE}/about", links["x-default"])
        self.assertEqual(f"{BASE}/fil/about", links["fil"])

    def test_trailing_slash_redirect_never_leaves_the_site(self):
        for path in ("//evil.example/", "/\\evil.example/", "///evil.example//"):
            with self.subTest(path=path):
                self.assertEqual("/evil.example", _redirect_target(path, path))

    def test_redirect_keeps_percent_encoding(self):
        self.assertEqual(
            "/maps%20x",
            _redirect_target("/maps x/", "/maps%20x/"),
        )


class PublicUrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # An origin hostname in the Host header must not leak into redirects.
        cls.client = TestClient(app, base_url="http://origin.invalid")

    def get(self, path, **kwargs):
        return self.client.get(path, follow_redirects=False, **kwargs)

    def assertRedirect(self, path, location, status=301):
        response = self.get(path)
        self.assertEqual(status, response.status_code, path)
        self.assertEqual(location, response.headers["location"], path)

    def test_trailing_slash_redirects_are_relative(self):
        self.assertRedirect("/about/", "/about")
        self.assertRedirect("/maps/?sort=name", "/maps?sort=name")
        self.assertRedirect("/admin/", "/admin")
        self.assertRedirect("/fi/about/", "/fi/about")

    def test_redirects_get_cache_and_security_headers(self):
        response = self.get("/about/")
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual("nosniff", response.headers["x-content-type-options"])

    def test_trailing_slash_is_not_redirected_for_other_methods(self):
        response = self.client.post("/api/reservations/", follow_redirects=False)
        self.assertEqual(404, response.status_code)
        self.assertNotIn("location", response.headers)

    def test_locale_home_and_english_prefix_are_normalised(self):
        self.assertRedirect("/fi", "/fi/")
        self.assertRedirect("/en", "/")
        self.assertRedirect("/en/about", "/about")
        self.assertRedirect("/en/about/", "/about")

    def test_localized_page(self):
        response = self.get("/fi/about")
        self.assertEqual(200, response.status_code)
        html = response.text
        self.assertIn('<html lang="fi">', html)
        self.assertEqual(f"{BASE}/fi/about", canonical(html))
        links = hreflangs(html)
        self.assertEqual(len(SUPPORTED_LOCALES) + 1, len(links))
        self.assertEqual(f"{BASE}/about", links["x-default"])
        self.assertEqual(f"{BASE}/sv/about", links["sv"])
        self.assertNotIn(NOINDEX, html)
        # Links stay in the URL language.
        self.assertIn('href="/fi/stats"', html)
        self.assertIn('href="/fi/"', html)

    def test_localized_home_page(self):
        response = self.get("/fi/")
        self.assertEqual(200, response.status_code)
        self.assertIn('<html lang="fi">', response.text)
        self.assertEqual(f"{BASE}/fi/", canonical(response.text))

    def test_url_locale_beats_cookie(self):
        response = self.get("/fi/about", headers={"Cookie": "lang=sv"})
        self.assertIn('<html lang="fi">', response.text)
        self.assertNotIn("Cookie", response.headers.get("vary", ""))

    def test_unprefixed_page_still_negotiates(self):
        response = self.get("/about", headers={"Cookie": "lang=sv"})
        self.assertIn('<html lang="sv">', response.text)
        self.assertEqual(f"{BASE}/about", canonical(response.text))
        vary = response.headers.get("vary", "")
        self.assertIn("Accept-Language", vary)
        self.assertIn("Cookie", vary)
        # Links stay unprefixed; the cookie keeps choosing the language.
        self.assertIn('href="/stats"', response.text)

    def test_only_indexable_pages_have_localized_copies(self):
        for path in ("/fi/bans", "/fi/feedback", "/fi/api/status", "/fi/my-reservations"):
            with self.subTest(path=path):
                self.assertEqual(404, self.get(path).status_code)

    def test_indexable_pages_have_canonical_and_no_noindex(self):
        for path in ("/", "/about", "/stats", "/maps", "/ping"):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(200, response.status_code)
                self.assertEqual(f"{BASE}{path}", canonical(response.text))
                self.assertNotIn(NOINDEX, response.text)

    def test_other_pages_are_noindex_without_canonical(self):
        for path in ("/bans", "/feedback", "/does-not-exist"):
            with self.subTest(path=path):
                html = self.get(path).text
                self.assertIn(NOINDEX, html)
                self.assertIsNone(canonical(html))
                self.assertEqual({}, hreflangs(html))

    def test_404_template_is_noindex_on_any_path(self):
        request = SimpleNamespace(
            state=SimpleNamespace(locale="en"),
            url=SimpleNamespace(path="/about"),
        )
        html = pages.templates.get_template("404.html").render(request=request, user=None)
        self.assertIn(NOINDEX, html)
        self.assertIsNone(canonical(html))

    def test_sitemap_lists_every_language_of_indexable_pages(self):
        response = self.get("/sitemap.xml")
        root = ElementTree.fromstring(response.content)
        urls = root.findall("sm:url", SITEMAP_NS)
        locs = {url.findtext("sm:loc", namespaces=SITEMAP_NS) for url in urls}
        expected = {
            BASE + localized_path(path, locale)
            for path in INDEXABLE_PATHS
            for locale in SUPPORTED_LOCALES
        }
        self.assertEqual(expected, locs)
        self.assertIn(f"{BASE}/fi/about", locs)
        self.assertFalse(any("/bans" in loc for loc in locs))
        for url in urls:
            self.assertIsNone(url.find("sm:lastmod", SITEMAP_NS))
            self.assertEqual(
                len(SUPPORTED_LOCALES) + 1,
                len(url.findall("xhtml:link", SITEMAP_NS)),
            )

    def test_robots_blocks_private_and_auth_paths(self):
        body = self.get("/robots.txt").text
        for path in ("/motd/", "/internal/", "/login", "/auth/", "/feedback"):
            self.assertIn(f"Disallow: {path}\n", body)
        # Crawlers must be able to fetch /bans to see its noindex.
        self.assertNotIn("Disallow: /bans", body)

    def test_my_reservations_redirects_to_login_when_logged_out(self):
        self.assertRedirect("/my-reservations", "/login", status=302)

    def test_favicon(self):
        self.assertEqual(404, self.get("/favicon.ico").status_code)
        with patch.object(pages.settings, "favicon_url", "https://cdn.example/icon.svg"):
            self.assertRedirect("/favicon.ico", "https://cdn.example/icon.svg", status=302)

    def test_language_switch_keeps_the_url_language_space(self):
        cases = [
            ("/fi/about", "sv", "/sv/about"),
            ("/fi/about", "en", "/about"),
            ("/fi/", "ja", "/ja/"),
            ("/about", "sv", "/about"),
            ("/fi/stats?tab=ping", "ko", "/ko/stats?tab=ping"),
            ("//evil.example", "fi", "/evil.example"),
        ]
        for referer_path, lang, location in cases:
            with self.subTest(referer=referer_path, lang=lang):
                response = self.client.post(
                    "/set-language",
                    data={"lang": lang},
                    headers={"referer": BASE + referer_path},
                    follow_redirects=False,
                )
                self.assertEqual(303, response.status_code)
                self.assertEqual(location, response.headers["location"])
