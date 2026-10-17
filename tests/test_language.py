"""The deterministic language path: words -> command proposal (no model)."""
import unittest

from companion.coordinator import COMMAND_VALIDATOR
from companion.language import ChainInterpreter, RuleInterpreter, normalize

FETCH = {"version": 1, "command": "fetch_pair"}
STOP = {"version": 1, "command": "stop"}
STATUS = {"version": 1, "command": "status"}


class RuleInterpreterTests(unittest.TestCase):
    rules = RuleInterpreter()

    def check(self, cases):
        for text, expected in cases:
            with self.subTest(text=text):
                proposal = self.rules.interpret(text)
                self.assertEqual(proposal, expected)
                if proposal is not None:
                    self.assertTrue(COMMAND_VALIDATOR.is_valid(proposal), proposal)

    def test_fetching_the_pair(self):
        self.check([
            ("bring me a tractor and forage wagon", FETCH),
            ("Bring me a tractor and forage wagon.", FETCH),
            ("Farmhand, go grab an available tractor and forage wagon, hook them together, "
             "and bring them here.", FETCH),
            ("hey farmhand can you get me the tractor with the loader wagon", FETCH),
            ("I need a tractor and a forage wagon over here please", FETCH),
            ("tractor and wagon", FETCH),
            ("bring the forage wagon and a tractor to me", FETCH),
        ])

    def test_destinations_are_extracted_never_invented(self):
        self.check([
            ("bring a tractor and forage wagon to the Home Yard",
             {**FETCH, "target": {"type": "place", "name": "Home Yard"}}),
            ("bring a tractor and forage wagon to the bus stop please",
             {**FETCH, "target": {"type": "place", "name": "bus stop"}}),
            ("bring a tractor and forage wagon to field 12",
             {**FETCH, "target": {"type": "field_entrance", "field": 12}}),
            ("bring a tractor and forage wagon to field number 7",
             {**FETCH, "target": {"type": "field_entrance", "field": 7}}),
            ("bring a tractor and forage wagon to me", FETCH),
            ("bring a tractor and forage wagon over here", FETCH),
        ])

    def test_moving_the_pair_already_out_there(self):
        MOVE = {"version": 1, "command": "move_pair"}
        barn = {**MOVE, "target": {"type": "place", "name": "barn"}}
        self.check([
            ("come here", MOVE), ("Farmhand, come here!", MOVE), ("come to me", MOVE), ("come back", MOVE),
            ("bring it here", MOVE), ("bring that over here", MOVE), ("bring them back here", MOVE),
            ("take this back to the barn", barn), ("take it to the barn", barn), ("park it at the barn", barn),
            ("drive this to the Machine Shed please", {**MOVE, "target": {"type": "place", "name": "Machine Shed"}}),
            ("take the tractor and wagon back to the barn", barn),
            ("take it to field 4", {**MOVE, "target": {"type": "field_entrance", "field": 4}}),
            ("take it", None),                          # where to? not guessed
            ("this is nice", None),
        ])

    def test_stop_is_deterministic_and_wins(self):
        self.check([
            ("stop", STOP), ("STOP!", STOP), ("Stop it", STOP), ("farmhand stop", STOP),
            ("hey farmhand, cancel", STOP), ("cancel that", STOP), ("abort", STOP),
            ("halt", STOP), ("hold on", STOP), ("never mind", STOP), ("please stop", STOP),
            ("stop bringing the tractor and forage wagon", STOP),
        ])

    def test_status_queries(self):
        self.check([
            ("what are you doing?", STATUS), ("Farmhand, what's happening", STATUS),
            ("status", STATUS), ("where are you", STATUS), ("how's it going", STATUS),
            ("are you done yet", STATUS),
        ])

    def test_anything_else_is_not_understood(self):
        self.check([
            ("", None), ("   ", None), ("make me a sandwich", None), ("tractor", None),
            ("bring me a tractor", None),               # the pair is the only fetch supported
            ("plow field 3", None), ("sell the forage wagon", None),
        ])
        self.assertIsNone(self.rules.interpret(None))
        self.assertIsNone(self.rules.interpret(42))

    def test_normalization(self):
        self.assertEqual(normalize("Hey  Farmhand,   PLEASE stop!!"), "stop")
        self.assertEqual(normalize("what's up"), "what s up")


class ChainTests(unittest.TestCase):
    def test_rules_first_then_a_model_and_a_broken_model_is_ignored(self):
        calls = []

        class Model:
            def interpret(self, text):
                calls.append(text)
                return {"version": 1, "command": "status"}

        class Broken:
            def interpret(self, text):
                raise RuntimeError("model crashed")

        chain = ChainInterpreter(RuleInterpreter(), Model())
        self.assertEqual(chain.interpret("stop"), STOP)
        self.assertEqual(calls, [])                         # STOP never waits for a model
        self.assertEqual(chain.interpret("how far along are you"), STATUS)
        self.assertEqual(calls, ["how far along are you"])
        self.assertIsNone(ChainInterpreter(RuleInterpreter(), Broken()).interpret("sing a song"))


class CommandSchemaTests(unittest.TestCase):
    def test_the_command_schema_admits_no_authority_or_control(self):
        bad = [{}, {"version": 2, "command": "stop"}, {"version": 1, "command": "drive"},
               {"version": 1, "command": "stop", "target": "here"},
               {"version": 1, "command": "fetch_pair", "vehicle_id": "11"},
               {"version": 1, "command": "fetch_pair", "farm_id": "1"},
               {"version": 1, "command": "fetch_pair", "target": {"x": 1, "z": 2}},
               {"version": 1, "command": "fetch_pair", "target": {"type": "field_entrance", "field": 0}}]
        for proposal in bad:
            with self.subTest(proposal=proposal):
                self.assertFalse(COMMAND_VALIDATOR.is_valid(proposal))


if __name__ == "__main__":
    unittest.main()
