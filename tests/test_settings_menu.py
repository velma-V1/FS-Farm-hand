"""Farmhand's settings screen (companion/ai.py SettingsMenu): the user, 2026-10-30 - a section
reached from the command window, pay modes and amounts, teleport recovery, buying only when
told, the harvest destination, preferred equipment and the AI model; every list fits GIANTS'
option dialog and every pick is saved to settings.json."""
from pathlib import Path
import tempfile
import unittest

from companion.ai import MAX_MENU, Settings, SettingsMenu


class SettingsMenuTests(unittest.TestCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        self.settings = Settings(folder / "settings.json")
        self.changes, self.ai_opened = [], []
        self.menu = SettingsMenu(self.settings, ai_menu=lambda: self.ai_opened.append(True),
                                 on_change=lambda: self.changes.append(True))

    def test_every_list_fits_the_games_option_dialog(self):
        for key in ("main", "pay", "harvest", "preferences"):
            title, labels = self.menu.open(key)
            self.assertTrue(title)
            self.assertLessEqual(len(labels), MAX_MENU, key)
            self.assertTrue(all(0 < len(label) <= 60 for label in labels), (key, labels))
        for mode_index in (1, 2, 3):
            self.menu.choose("pay", mode_index)
            self.assertLessEqual(len(self.menu.open("pay_amount")[1]), MAX_MENU)

    def test_the_main_list_says_each_current_setting(self):
        labels = self.menu.open("main")[1]
        self.assertEqual(labels[0], "Pay: per hour at GIANTS' rate")
        self.assertEqual(labels[1], "Teleport a stuck rig: on")
        self.assertEqual(labels[2], "Buying: buys only when told")
        self.assertEqual(labels[-1], "Close")

    def test_pay_is_chosen_by_mode_then_amount_and_saved(self):
        self.assertEqual(self.menu.choose("main", 1), "pay")
        self.assertEqual(self.menu.choose("pay", 3), "pay_amount")           # per day
        amounts = self.menu.open("pay_amount")[1]
        self.assertEqual(self.menu.choose("pay_amount", amounts.index("2,500") + 1), "main")
        self.assertEqual(self.settings.pay(), (3, 2500.0))
        self.assertEqual(self.menu.open("main")[1][0], "Pay: 2,500 per day")
        self.assertEqual(self.menu.choose("pay", 5), "main")                 # the game's own wages
        self.assertEqual(self.settings.pay(), (4, 0.0))
        self.assertEqual(len(self.changes), 2)                                # each applied at once

    def test_toggles_and_the_harvest_destination_are_saved(self):
        self.assertEqual(self.menu.choose("main", 2), "main")
        self.assertFalse(self.settings.teleport_recovery())
        self.assertEqual(self.menu.choose("main", 3), "main")
        self.assertTrue(self.settings.load()["buy_when_needed"])
        self.assertEqual(self.menu.choose("main", 4), "harvest")
        self.assertEqual(self.menu.choose("harvest", 1), "main")
        self.assertEqual(self.settings.harvest_destination(), "sell")

    def test_the_ai_model_and_close_leave_the_settings_list(self):
        self.assertIsNone(self.menu.choose("main", 6))
        self.assertEqual(self.ai_opened, [True])
        self.assertIsNone(self.menu.choose("main", 7))
        self.assertEqual(self.menu.choose("pay", 6), "main")                 # Back
        self.assertEqual(self.menu.choose("main", 99), "main")               # out of range: stays


if __name__ == "__main__":
    unittest.main()
