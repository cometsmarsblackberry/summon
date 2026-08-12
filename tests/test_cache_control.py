import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.static_assets import static_asset_url


class CacheControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_stable_static_asset_is_revalidated(self):
        response = self.client.get("/static/css/styles.css")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "no-cache, max-age=0, must-revalidate",
            response.headers["cache-control"],
        )

    def test_fingerprinted_static_asset_is_immutable(self):
        asset_url = static_asset_url("css/styles.css")

        self.assertRegex(
            asset_url,
            r"^/static/v-[0-9a-f]{64}/css/styles\.css$",
        )

        response = self.client.get(asset_url)
        stable_response = self.client.get("/static/css/styles.css")

        self.assertEqual(200, response.status_code)
        self.assertEqual(stable_response.content, response.content)
        self.assertEqual(
            "public, max-age=31536000, immutable",
            response.headers["cache-control"],
        )

    def test_stale_fingerprint_is_not_served_or_cached(self):
        asset_url = static_asset_url("css/styles.css")
        fingerprint = asset_url.split("/", 4)[2].removeprefix("v-")
        stale_fingerprint = (
            ("0" if fingerprint[0] != "0" else "1") + fingerprint[1:]
        )
        stale_url = asset_url.replace(fingerprint, stale_fingerprint, 1)

        response = self.client.get(stale_url)

        self.assertEqual(404, response.status_code)
        self.assertEqual("no-store", response.headers["cache-control"])

    def test_missing_static_asset_is_not_cacheable(self):
        response = self.client.get("/static/does-not-exist")

        self.assertEqual(404, response.status_code)
        self.assertEqual("no-store", response.headers["cache-control"])

    def test_mutable_bootstrap_assets_are_not_cacheable(self):
        for path in ("/static/install-instant-host.sh", "/static/tf2-agent"):
            with self.subTest(path=path):
                response = self.client.head(path)

                self.assertEqual(200, response.status_code)
                self.assertEqual("no-store", response.headers["cache-control"])


if __name__ == "__main__":
    unittest.main()
