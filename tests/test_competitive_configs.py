import json
import unittest

from app.services.competitive_configs import group_for_ui


class GroupForUiTests(unittest.TestCase):
    def test_popular_leagues_are_listed_first(self):
        grouped = group_for_ui([
            "ultitrio_koth",
            "cltf2_4s_koth",
            "tfarena_6v6_koth",
            "fbtf_6v6_5cp",
            "ozfortress_6v6_5cp",
            "rgl_6s_5cp_scrim",
            "etf2l_6v6_5cp",
        ])

        expected = ["ETF2L", "RGL", "ozfortress", "FBTF", "CLTF2", "TFArena", "Ultitrio"]
        self.assertEqual(list(grouped), expected)
        # The frontend relies on JSON key order surviving serialization.
        self.assertEqual(list(json.loads(json.dumps(grouped))), expected)

    def test_missing_priority_leagues_are_skipped(self):
        grouped = group_for_ui(["ultitrio_koth", "cltf2_4s_koth", "rgl_6s_5cp_scrim"])

        self.assertEqual(list(grouped), ["RGL", "CLTF2", "Ultitrio"])


if __name__ == "__main__":
    unittest.main()
