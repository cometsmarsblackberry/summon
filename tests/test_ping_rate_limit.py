import unittest
from unittest.mock import patch

from app.routers import ping


class PingRateLimitTests(unittest.TestCase):
    def setUp(self):
        ping._submit_timestamps.clear()
        ping._sitewide_submit_timestamps.clear()
        self.addCleanup(ping._submit_timestamps.clear)
        self.addCleanup(ping._sitewide_submit_timestamps.clear)

    def submit(self, ip: str) -> bool:
        if not ping._check_rate_limit(ip):
            return False
        ping._record_submission(ip)
        return True

    def test_ipv6_addresses_in_one_slash_64_share_a_limit(self):
        for host in range(ping.SUBMIT_LIMIT):
            self.assertTrue(self.submit(f"2001:db8:1:2::{host + 1:x}"))
        self.assertFalse(self.submit("2001:db8:1:2:ffff::1"))
        self.assertTrue(self.submit("2001:db8:1:3::1"))

    def test_ipv4_mapped_addresses_count_as_ipv4(self):
        for _ in range(ping.SUBMIT_LIMIT):
            self.assertTrue(self.submit("198.51.100.7"))
        self.assertFalse(self.submit("::ffff:198.51.100.7"))

    def test_submissions_from_many_networks_hit_the_sitewide_cap(self):
        for network in range(ping.SITEWIDE_SUBMIT_LIMIT):
            self.assertTrue(self.submit(f"2001:db8:{network:x}::1"))
        self.assertFalse(self.submit("203.0.113.9"))

    def test_checks_alone_store_nothing(self):
        for network in range(1000):
            ping._check_rate_limit(f"2001:db8:{network:x}::1")
        self.assertEqual({}, ping._submit_timestamps)

    def test_clients_are_forgotten_after_the_window(self):
        self.assertTrue(self.submit("198.51.100.7"))
        later = ping.time.time() + ping.SUBMIT_WINDOW + 1
        with patch.object(ping.time, "time", return_value=later):
            self.assertTrue(ping._check_rate_limit("203.0.113.9"))
        self.assertEqual({}, ping._submit_timestamps)
        self.assertEqual([], ping._sitewide_submit_timestamps)


if __name__ == "__main__":
    unittest.main()
