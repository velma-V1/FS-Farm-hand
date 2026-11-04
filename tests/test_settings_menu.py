"""Farmhand's settings screen (companion/ai.py SettingsMenu): the user, 2026-10-30 - a section
reached from the command window, pay modes and amounts, buying only when told, the spending
limit, the harvest destination, teleport recovery, passing through scenery and the AI model;
every list fits GIANTS' option dialog, every pick is saved to settings.json, and only settings
Farmhand implements are listed (AGENTS.md "Player-facing settings contract")."""
from pathlib import Path
import tempfile
import unittest

from companion.ai import MAX_MENU, SPENDING_LIMITS, Settings, SettingsMenu


class SettingsMenuTests(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        self.settings = Settings(folder / "settings.json")
        self.changes, self.ai_opened = [], []
        self.menu = SettingsMenu(self.settings, ai_menu=lambda: self.ai_opened.append(True),
                                 on_change=lambda: self.changes.append(True))

    def index(self, entry):
        """The 1-based line of a main-list entry, as the player picks it."""
        return [e for e, _ in self.menu.main_entries()].index(entry) + 1

    def label(self, entry):
        return self.menu.open("main")[1][self.index(entry) - 1]

    def pick(self, entry):
        return self.menu.choose("main", self.index(entry))

    def test_every_list_fits_the_games_option_dialog(self):
        for key in ("main", "pay", "limit", "harvest", "developer"):
            title, labels = self.menu.open(key)
            self.assertTrue(title)
            self.assertLessEqual(len(title), 60, key)
            self.assertLessEqual(len(labels), MAX_MENU, key)
            self.assertTrue(all(0 < len(label) <= 60 for label in labels), (key, labels))
        for mode_index in (1, 2, 3):
            self.menu.choose("pay", mode_index)
            self.assertLessEqual(len(self.menu.open("pay_amount")[1]), MAX_MENU)

    def test_the_main_list_says_each_current_setting_grouped_by_topic(self):
        labels = self.menu.open("main")[1]
        self.assertEqual(labels, ["Pay: per hour at GIANTS' rate", "Buying: buys only when told",
                                  "Spending limit: none", "Harvest: keep it in the trailer",
                                  "Teleport a stuck rig: on", "Pass through scenery in tight spots: off",
                                  "Developer options", "AI model", "Close"])

    def test_only_implemented_settings_are_listed(self):
        # Nothing saves or reads equipment preferences yet: no entry claims to (AGENTS.md).
        self.settings.save({"preferences": [{"text": "old entry"}]})
        self.assertFalse(any("Preferred" in label for label in self.menu.open("main")[1]))
        with self.assertRaises(KeyError):
            self.menu.open("preferences")

    def test_pay_is_chosen_by_mode_then_amount_and_saved(self):
        self.assertEqual(self.pick("pay"), "pay")
        self.assertEqual(self.menu.choose("pay", 3), "pay_amount")           # per day
        amounts = self.menu.open("pay_amount")[1]
        self.assertEqual(self.menu.choose("pay_amount", amounts.index("2,500") + 1), "main")
        self.assertEqual(self.settings.pay(), (3, 2500.0))
        self.assertEqual(self.label("pay"), "Pay: 2,500 per day")
        self.assertEqual(self.menu.choose("pay", 5), "main")                 # the game's own wages
        self.assertEqual(self.settings.pay(), (4, 0.0))
        self.assertEqual(len(self.changes), 2)                                # each applied at once

    def test_toggles_and_the_harvest_destination_are_saved(self):
        self.assertEqual(self.pick("teleport"), "main")
        self.assertFalse(self.settings.teleport_recovery())
        self.assertEqual(self.pick("buying"), "main")
        self.assertTrue(self.settings.load()["buy_when_needed"])
        self.assertEqual(self.label("buying"), "Buying: buys what a job needs")
        self.assertEqual(self.pick("harvest"), "harvest")
        self.assertEqual(self.menu.choose("harvest", 1), "main")
        self.assertEqual(self.settings.harvest_destination(), "sell")

    def test_passing_through_scenery_is_off_until_the_player_turns_it_on(self):
        # AGENTS.md 10: the early building ghosting is never imposed; only the player's choice.
        self.assertFalse(self.settings.scenery_ghosting())
        for stored in ("yes", 1, None, {"on": True}):                         # only exactly true is on
            self.settings.save({"scenery_ghosting": stored})
            self.assertFalse(self.settings.scenery_ghosting(), stored)
        self.settings.save({})
        self.assertEqual(self.pick("ghosting"), "main")
        self.assertIs(self.settings.load()["scenery_ghosting"], True)
        self.assertTrue(self.settings.scenery_ghosting())
        self.assertEqual(self.label("ghosting"), "Pass through scenery in tight spots: on")
        self.assertEqual(self.pick("ghosting"), "main")                       # and off again
        self.assertFalse(self.settings.scenery_ghosting())
        self.assertEqual(len(self.changes), 2)                                # applied at once each time
        self.assertTrue(self.settings.teleport_recovery())                    # nothing else changed

    def test_the_spending_limit_is_chosen_from_presets_and_none_by_default(self):
        self.assertIsNone(self.settings.spending_limit())
        self.assertEqual(self.pick("limit"), "limit")
        title, labels = self.menu.open("limit")
        self.assertEqual(labels[0], "No limit")
        self.assertEqual(labels[-1], "Back")
        self.assertEqual(self.menu.choose("limit", labels.index("50,000") + 1), "main")
        self.assertEqual(self.settings.spending_limit(), 50000.0)
        self.assertEqual(self.label("limit"), "Spending limit: 50,000 a purchase")
        self.assertEqual(self.changes, [True])                                # applied at once
        self.assertEqual(self.menu.choose("limit", 1), "main")               # No limit again
        self.assertIsNone(self.settings.spending_limit())
        self.assertNotIn("spending_limit", self.settings.load())
        self.assertEqual(self.menu.choose("limit", len(labels)), "main")     # Back: unchanged
        self.assertEqual(len(self.changes), 2)
        self.assertEqual(len(labels), len(SPENDING_LIMITS) + 1)

    def test_only_a_positive_amount_is_a_spending_limit(self):
        for stored in (0, -5, "50000", True, None, float("nan"), 1e12):
            self.settings.save({"spending_limit": stored})
            self.assertIsNone(self.settings.spending_limit(), stored)
        self.settings.save({"spending_limit": 25000})
        self.assertEqual(self.settings.spending_limit(), 25000.0)
        for bad in (0, -1, "1000", True, 1e12):
            with self.assertRaises(ValueError):
                self.settings.set_spending_limit(bad)
        self.assertEqual(self.settings.spending_limit(), 25000.0)             # a refused value changes nothing

    def test_the_navigation_test_is_a_visible_developer_option_off_by_default(self):
        # It replaced the hidden nav_proof.flag file: the mode is never on without the screen saying so.
        self.assertFalse(self.settings.navigation_proof())
        self.assertEqual(self.pick("developer"), "developer")
        title, labels = self.menu.open("developer")
        self.assertEqual(labels, ["Farmhand's own driving only (navigation test): off", "Back"])
        self.assertEqual(self.menu.choose("developer", 1), "developer")   # toggled, the list shown again
        self.assertTrue(self.settings.navigation_proof())
        self.assertEqual(self.menu.open("developer")[1][0], "Farmhand's own driving only (navigation test): on")
        self.assertEqual(self.label("developer"), "Developer options: navigation test on")
        self.assertEqual(self.menu.choose("developer", 2), "main")        # Back
        self.assertEqual(self.menu.choose("developer", 1), "developer")
        self.assertFalse(self.settings.navigation_proof())
        self.assertEqual(len(self.changes), 2)
        for stored in ("yes", 1, None):
            self.settings.save({"navigation_proof": stored})
            self.assertFalse(self.settings.navigation_proof(), stored)

    def test_the_ai_model_and_close_leave_the_settings_list(self):
        self.assertIsNone(self.pick("ai"))
        self.assertEqual(self.ai_opened, [True])
        self.assertIsNone(self.pick("close"))
        self.assertEqual(self.menu.choose("pay", 6), "main")                 # Back
        self.assertEqual(self.menu.choose("main", 99), "main")               # out of range: stays

    def test_each_main_entry_leads_where_its_label_says(self):
        entries = [entry for entry, _ in self.menu.main_entries()]
        self.assertEqual(len(entries), len(set(entries)))
        self.assertEqual(entries[-1], "close")
        for entry in entries:
            if entry in ("pay", "limit", "harvest", "developer"):
                self.assertEqual(self.pick(entry), entry)
                self.menu.open(entry)                                         # its list exists


if __name__ == "__main__":
    unittest.main()
