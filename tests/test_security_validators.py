import unittest

from app.utils.maps import is_valid_map_name
from app.utils.upload_links import is_allowed_upload_url


class UploadUrlValidationTests(unittest.TestCase):
    def test_accepts_expected_https_hosts(self):
        self.assertTrue(is_allowed_upload_url("https://logs.tf/12345", "log"))
        self.assertTrue(is_allowed_upload_url("https://www.demos.tf/abcdef", "demo"))

    def test_rejects_wrong_scheme_even_when_hostname_matches(self):
        self.assertFalse(is_allowed_upload_url("http://logs.tf/12345", "log"))
        self.assertFalse(is_allowed_upload_url("javascript://logs.tf/%0Aalert(1)", "log"))

    def test_rejects_wrong_host(self):
        self.assertFalse(is_allowed_upload_url("https://evil.example/upload", "log"))


class MapNameValidationTests(unittest.TestCase):
    def test_accepts_standard_map_names(self):
        self.assertTrue(is_valid_map_name("cp_process_f12"))
        self.assertTrue(is_valid_map_name("koth_product_rcx"))

    def test_rejects_injection_primitives(self):
        self.assertFalse(is_valid_map_name("cp_process; quit"))
        self.assertFalse(is_valid_map_name("cp_process\nchangelevel badlands"))
        self.assertFalse(is_valid_map_name("../cp_badlands"))


if __name__ == "__main__":
    unittest.main()
