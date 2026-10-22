"""tests/scenarios.json: Farmhand's acceptance scenarios in the player's own words (run live by
scripts/fs25_scenarios.py). Offline, every sentence must be understood by the deterministic rules
alone (no model: a 1-4B local model only rewrites wording into these forms) into a valid command,
and every mock test a scenario names must exist, so the live list and the tests cannot drift."""
import importlib
import json
from pathlib import Path
import unittest

from companion.coordinator import COMMAND_VALIDATOR
from companion.language import RuleInterpreter

SCENARIOS = json.loads((Path(__file__).parent / "scenarios.json").read_text())["scenarios"]


class ScenarioTests(unittest.TestCase):
    def test_every_sentence_is_understood_by_the_rules_alone(self):
        rules = RuleInterpreter()
        for scenario in SCENARIOS:
            for say in scenario["say"]:
                with self.subTest(scenario=scenario["id"], say=say):
                    proposal = rules.interpret(say)
                    if not scenario["implemented"]:
                        continue                  # reported live as not yet built, never parsed into something else
                    self.assertIsNotNone(proposal)
                    self.assertTrue(COMMAND_VALIDATOR.is_valid(proposal), proposal)
                    self.assertNotEqual(proposal["command"], "unsupported", proposal)

    def test_every_named_covering_test_exists(self):
        for scenario in SCENARIOS:
            for name in scenario["covered_by"]:
                with self.subTest(scenario=scenario["id"], test=name):
                    module, _, attribute = name.rpartition(".")
                    if module.startswith("tests.test_") and "." not in module[len("tests."):]:
                        self.assertTrue(hasattr(importlib.import_module(module), attribute), name)
                    else:
                        importlib.import_module(name)

    def test_shape(self):
        ids = [s["id"] for s in SCENARIOS]
        self.assertEqual(len(ids), len(set(ids)))
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario["id"]):
                self.assertEqual(len(scenario["say"]), len(scenario["expect"]))
                self.assertTrue(scenario["saves"] == "any" or all(isinstance(n, int) for n in scenario["saves"]))
                self.assertTrue(scenario["implemented"] or not scenario["covered_by"])
