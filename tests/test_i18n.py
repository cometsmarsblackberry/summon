import json
import re
import unittest
from pathlib import Path


LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
PROJECT_ROOT = LOCALES_DIR.parent
PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")


def flatten(data: dict, prefix: str = "") -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            values.update(flatten(value, full_key))
        else:
            values[full_key] = str(value)
    return values


def load_catalog(path: Path) -> dict[str, str]:
    def reject_duplicate_keys(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError(f"duplicate translation key {key!r} in {path.name}")
            values[key] = value
        return values

    return flatten(json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
    ))


class TranslationCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.english = load_catalog(LOCALES_DIR / "en.json")
        cls.translations = {
            path.stem: load_catalog(path)
            for path in sorted(LOCALES_DIR.glob("*.json"))
            if path.name != "en.json"
        }

    def test_catalogs_match_english_keys(self):
        expected = set(self.english)
        for locale, catalog in self.translations.items():
            with self.subTest(locale=locale):
                self.assertEqual(expected, set(catalog))

    def test_catalog_placeholders_match_english(self):
        for locale, catalog in self.translations.items():
            for key, english_value in self.english.items():
                with self.subTest(locale=locale, key=key):
                    self.assertEqual(
                        sorted(PLACEHOLDER_RE.findall(english_value)),
                        sorted(PLACEHOLDER_RE.findall(catalog[key])),
                    )

    def test_reservation_action_labels_stay_compact(self):
        expected = {
            "en": ("Restart", "End"),
            "es": ("Reiniciar", "Terminar"),
            "fi": ("Käynnistä uudelleen", "Lopeta"),
            "fil": ("I-restart", "Tapusin"),
            "ja": ("再起動", "終了"),
            "ko": ("재시작", "종료"),
            "ms": ("Mulakan Semula", "Tamatkan"),
            "pt": ("Reiniciar", "Encerrar"),
            "sv": ("Starta om", "Avsluta"),
            "th": ("รีสตาร์ท", "สิ้นสุด"),
            "vi": ("Khởi động lại", "Kết thúc"),
        }
        catalogs = {"en": self.english, **self.translations}

        self.assertEqual(set(expected), set(catalogs))
        for locale, labels in expected.items():
            with self.subTest(locale=locale):
                self.assertEqual(labels[0], catalogs[locale]["status.restart_server"])
                self.assertEqual(labels[1], catalogs[locale]["status.end_reservation"])

    def test_restored_english_operational_copy(self):
        expected = {
            "home.config_unavailable": (
                "Config list unavailable. You can still choose one after the server starts."
            ),
            "status.progress_downloading_layers": (
                "Downloading layers ({current} / {total})..."
            ),
            "status.progress_extracting_layers": (
                "Extracting layers ({current} / {total})..."
            ),
            "status.progress_preparing_container": "Preparing container image...",
            "status.progress_using_cached_layers": "Using cached image layers...",
            "status.progress_downloading_layers_count": "Downloading layers ({count})...",
            "status.progress_retrying_image_download": (
                "Retrying image download (attempt {attempt})..."
            ),
            "status.progress_container_pulled": "Image downloaded",
            "stats.reservation_load_failed": "Failed to load reservation stats.",
            "stats.ping_load_failed": "Failed to load ping stats.",
        }

        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(value, self.english[key])


class FrontendLocaleTests(unittest.TestCase):
    def test_live_image_progress_uses_localized_agent_details(self):
        template = (PROJECT_ROOT / "templates" / "status.html").read_text(
            encoding="utf-8"
        )

        for key in (
            "progress_downloading_layers",
            "progress_extracting_layers",
            "progress_preparing_container",
            "progress_using_cached_layers",
            "progress_downloading_layers_count",
            "progress_retrying_image_download",
        ):
            with self.subTest(key=key):
                self.assertIn(f"_('status.{key}')", template)

        self.assertIn("message.match(/^Downloading layers", template)
        self.assertIn("message.match(/^Extracting layers", template)
        self.assertNotIn("bootProgress ? bootProgress.message : ''", template)

    def test_statistics_keep_specific_localized_load_errors(self):
        template = (PROJECT_ROOT / "templates" / "stats.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("_('stats.reservation_load_failed')", template)
        self.assertIn("_('stats.ping_load_failed')", template)

    def test_date_views_use_the_site_locale_instead_of_browser_default(self):
        for template_name in (
            "my_reservations.html",
            "profile.html",
            "stats.html",
            "status.html",
        ):
            with self.subTest(template=template_name):
                template = (PROJECT_ROOT / "templates" / template_name).read_text(
                    encoding="utf-8"
                )
                self.assertIn("summonIntl.", template)
                self.assertNotRegex(
                    template,
                    r"toLocale(?:String|DateString|TimeString)\(\[\]",
                )

    def test_shared_formatter_is_loaded_by_standard_and_motd_layouts(self):
        for template_name in ("base.html", "motd.html"):
            with self.subTest(template=template_name):
                template = (PROJECT_ROOT / "templates" / template_name).read_text(
                    encoding="utf-8"
                )
                self.assertIn("static_asset('js/intl.js')", template)

        formatter = (PROJECT_ROOT / "static" / "js" / "intl.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("language === 'en' ? 'en-GB'", formatter)
        self.assertIn("new Intl.DateTimeFormat(locale(language)", formatter)


if __name__ == "__main__":
    unittest.main()
