"""Tests for bot/config_defaults.py — the shared ani.json settings defaults
used by both the bot (loadconfig) and the dashboard."""

import unittest

import support

config_defaults = support.load_config_defaults()


class DefaultAniDataTest(unittest.TestCase):
    def test_full_settings_and_empty_watchlist(self):
        data = config_defaults.default_ani_data()
        self.assertEqual(data["settings"], config_defaults.DEFAULT_SETTINGS)
        self.assertEqual(data["anime"], [])

    def test_returns_a_fresh_copy_each_call(self):
        first = config_defaults.default_ani_data()
        first["settings"]["jdhost"] = "mutated"
        first["anime"].append("mutated")
        second = config_defaults.default_ani_data()
        self.assertEqual(second["settings"]["jdhost"], "")
        self.assertEqual(second["anime"], [])


class FillSettingsDefaultsTest(unittest.TestCase):
    def test_empty_settings_gets_every_default_reported_missing(self):
        filled, missing = config_defaults.fill_settings_defaults({})
        self.assertEqual(filled, config_defaults.DEFAULT_SETTINGS)
        self.assertEqual(set(missing), set(config_defaults.DEFAULT_SETTINGS))

    def test_only_absent_keys_are_filled_and_reported(self):
        settings = {"jdhost": "127.0.0.1", "hoster": 0}
        filled, missing = config_defaults.fill_settings_defaults(settings)

        self.assertEqual(filled["jdhost"], "127.0.0.1")
        self.assertEqual(filled["hoster"], 0)
        self.assertEqual(filled["timedelay"], 600)
        self.assertNotIn("jdhost", missing)
        self.assertNotIn("hoster", missing)
        self.assertIn("timedelay", missing)

    def test_present_but_falsy_value_is_not_reported_missing(self):
        # An owner who deliberately cleared myjd_pw (or a config that was
        # never given one) must not be nagged about it as "missing" — that's
        # a real, intentional value, not an absent key.
        settings = dict(config_defaults.DEFAULT_SETTINGS)
        settings["myjd_pw"] = ""
        filled, missing = config_defaults.fill_settings_defaults(settings)
        self.assertEqual(filled["myjd_pw"], "")
        self.assertNotIn("myjd_pw", missing)

    def test_does_not_mutate_input(self):
        settings = {"jdhost": "127.0.0.1"}
        config_defaults.fill_settings_defaults(settings)
        self.assertEqual(settings, {"jdhost": "127.0.0.1"})


if __name__ == "__main__":
    unittest.main()
