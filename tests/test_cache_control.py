import unittest

from fastapi.testclient import TestClient

from app.main import app


class CacheControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_successful_static_asset_is_cacheable(self):
        response = self.client.get("/static/css/styles.css")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "public, max-age=2592000, immutable",
            response.headers["cache-control"],
        )

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
