import json
import unittest

from app.services.competitive_configs import classify_config, filter_user_selectable, group_for_ui


class AsiaFortressConfigTests(unittest.TestCase):
    def test_6s_and_highlander_configs_share_one_league(self):
        self.assertEqual(classify_config("afc_6s_5cp_match_pro"), ("AsiaFortress", "6v6"))
        self.assertEqual(classify_config("af_HL_koth"), ("AsiaFortress", "Highlander"))
        self.assertEqual(classify_config("af_HL_stopwatch"), ("AsiaFortress", "Highlander"))

    def test_base_config_is_not_selectable(self):
        selectable = filter_user_selectable([
            "af_HL_base",
            "af_HL_koth",
            "af_HL_koth_bo5",
            "afc_6s_5cp_match_pro",
        ])

        self.assertEqual(selectable, ["af_HL_koth", "af_HL_koth_bo5", "afc_6s_5cp_match_pro"])


class GroupForUiTests(unittest.TestCase):
    def test_popular_leagues_are_listed_first(self):
        grouped = group_for_ui([
            "ultitrio_koth",
            "cltf2_4s_koth",
            "tfarena_6v6_koth",
            "fbtf_6v6_5cp",
            "afc_6s_5cp_match_pro",
            "ozfortress_6v6_5cp",
            "rgl_6s_5cp_scrim",
            "etf2l_6v6_5cp",
        ])

        expected = ["ETF2L", "RGL", "ozfortress", "AsiaFortress", "FBTF", "CLTF2", "TFArena", "Ultitrio"]
        self.assertEqual(list(grouped), expected)
        # The frontend relies on JSON key order surviving serialization.
        self.assertEqual(list(json.loads(json.dumps(grouped))), expected)

    def test_missing_priority_leagues_are_skipped(self):
        grouped = group_for_ui(["ultitrio_koth", "cltf2_4s_koth", "rgl_6s_5cp_scrim"])

        self.assertEqual(list(grouped), ["RGL", "CLTF2", "Ultitrio"])


if __name__ == "__main__":
    unittest.main()
