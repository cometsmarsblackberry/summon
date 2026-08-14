import json
import re
import unittest
from pathlib import Path


LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
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


if __name__ == "__main__":
    unittest.main()
