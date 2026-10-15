import unittest

from app.routers.internal import agent_stats, get_agent_stats, handle_agent_message


class CloudAgentHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        agent_stats.pop("agent", None)

    async def test_heartbeat_keeps_running_image_digest(self):
        digest = "sha256:" + "0123456789abcdef" * 4
        await handle_agent_message("agent", {
            "type": "status",
            "sysinfo": {"num_cpus": 2},
            "image_digest": digest,
        })

        stats = get_agent_stats("agent")
        self.assertEqual(2, stats["num_cpus"])
        self.assertEqual(digest, stats["image_digest"])

    async def test_heartbeat_from_agent_without_digest(self):
        await handle_agent_message("agent", {
            "type": "status",
            "sysinfo": {"num_cpus": 2},
            "image_digest": {"not": "a digest"},
        })

        self.assertNotIn("image_digest", get_agent_stats("agent"))


if __name__ == "__main__":
    unittest.main()
