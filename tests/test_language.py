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
            ("grab something that'll pull that forage wagon and bring the setup over here", FETCH),
            ("fetch the forage wagon", FETCH),
        ])

    def test_other_implements_by_their_names(self):
        def hitch(kind):
            return {"version": 1, "command": "hitch_pair", "implement": kind}

        def fetch(kind, **target):
            return {"version": 1, "command": "fetch_pair", "implement": kind, **target}
        field3 = {"target": {"type": "field_entrance", "field": 3}}
        self.check([
            ("hitch the cultivator", hitch("cultivator")),
            ("hook up the plough", hitch("plow")),
            ("attach the seeder to a tractor", hitch("sowing_machine")),
            ("take the tractor to the sprayer", hitch("sprayer")),
            ("couple a tractor to the trailer", hitch("trailer")),
            ("hitch the fertilizer spreader", hitch("sprayer")),
            ("hook up the manure spreader", hitch("manure_barrel")),      # not the sprayer
            ("hitch the mixer wagon", hitch("mixer_wagon")),               # not the forage wagon
            ("bring me the cultivator", fetch("cultivator")),
            ("get the seed drill", fetch("sowing_machine")),
            ("hitch the mower and bring it here", fetch("mower")),
            ("bring the seeder to field 3", fetch("sowing_machine", **field3)),
            ("hook up the trailer and take it to the barn", fetch("trailer", target={"type": "place", "name": "barn"})),
            # Not orders, or not a hitch: never a job.
            ("don't hitch the plough", None),
            ("where is the seeder", None),
            ("plow field 12", None),                  # field work is not hitching
            ("the cultivator", None),
            # The forage wagon keeps its own rules.
            ("fetch the forage wagon", FETCH),
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

    def test_settings_and_report_words(self):
        self.check([(t, {"version": 1, "command": "settings"}) for t in ("choose AI model", "settings", "change model")]
                   + [(t, {"version": 1, "command": "report"}) for t in ("report", "save a report", "diagnostics")])

    def test_status_queries(self):
        self.check([
            ("what are you doing?", STATUS), ("Farmhand, what's happening", STATUS),
            ("status", STATUS), ("where are you", STATUS), ("how's it going", STATUS),
            ("are you done yet", STATUS),
        ])

    def test_negations_and_questions_never_move_anything(self):
        from companion.language import refuses
        never = ["don't bring the tractor and wagon", "I don't need the tractor and wagon",
                 "no, not the tractor and wagon", "where is the tractor and wagon",
                 "did you bring the tractor and wagon?", "why did you bring the tractor and wagon",
                 "do not take it back", "dont move it back", "never bring it back", "don't bring it here"]
        self.check([(text, None) for text in never])
        self.assertTrue(all(refuses(text) for text in never))
        self.assertFalse(refuses("stop"))
        self.assertFalse(refuses("what are you doing"))
        self.assertFalse(refuses("could you get the forage wagon hooked up and over to me"))

    def test_more_ways_to_say_stop(self):
        self.check([(text, STOP) for text in ("wait", "whoa", "hold it", "stay there", "stay put",
                                              "bring it back here and stop", "no stop")])

    def test_destination_is_the_place_not_the_words_around_it(self):
        barn = {"type": "place", "name": "barn"}
        self.check([
            ("bring the tractor and wagon to me at the barn", {**FETCH, "target": barn}),
            ("take it back to the barn in a minute", {"version": 1, "command": "move_pair", "target": barn}),
            ("take it back to the barn now please", {"version": 1, "command": "move_pair", "target": barn}),
            ("bring me a tractor and wagon to the field", None),      # which field? not guessed
            ("take it over there", None),
            ("could you bring me a tractor and forage wagon?", FETCH),
        ])

    def test_live_session_language_cases(self):
        # The first FS25 session, with the intended meaning of each sentence.
        farm = {"type": "place", "name": "farm"}
        HITCH = {"version": 1, "command": "hitch_pair"}
        TRACTOR_ONLY = {"version": 1, "command": "unsupported", "reason": "tractor_only"}
        self.check([
            ("bring the tractor and forage wagon from the store to the farm", {**FETCH, "target": farm}),
            ("bring the available tractor and forage wagon from the store to the farm", {**FETCH, "target": farm}),
            ("bring the tractor and wagon from the store to the farm", {**FETCH, "target": farm}),
            ("take the tractor and get the forage wagon from the store and park it at the farm",
             {**FETCH, "target": farm}),
            ("bring the Boss Hoss to the farm", {"version": 1, "command": "unsupported", "reason": "named_vehicle"}),
            ("take the tractor and attach the forage wagon", HITCH),
            ("deliver the tractor to the forage wagon", HITCH),       # equipment, never a place
            ("take a tractor to the wagon", HITCH),
            ("take a tractor to the store", TRACTOR_ONLY),          # never adds a wagon
            ("brin a tractor to me", TRACTOR_ONLY),
            ("bring a tractor to me", TRACTOR_ONLY),
            ("bring me a tractor", TRACTOR_ONLY),
            ("hook the tractor to the forage wagon", HITCH),
            ("get the tractor and hook up the forage wagon", HITCH),
            ("take the tractor to the forage wagon", HITCH),
            ("hook up the forage wagon and bring it to the barn",
             {**FETCH, "target": {"type": "place", "name": "barn"}}),
            ("hitch the tractor to the forage wagon then take it to the barn",
             {**FETCH, "target": {"type": "place", "name": "barn"}}),
            ("attach the forage wagon", HITCH),
            ("hook up the wagon", HITCH),
            ("hook up the wagon and bring it here", FETCH),
        ])

    def test_equipment_is_never_a_destination_and_statements_are_not_orders(self):
        # Review of the first corrective pass: these must not start a hitch.
        self.check([
            ("stay at the wagon", None), ("go to the wagon", None), ("drive to the wagon", None),
            ("leave the tractor at the wagon", None), ("park the tractor next to the wagon", None),
            ("i parked the tractor next to the wagon", None), ("the tractor goes to the wagon", None),
            ("i already attached the tractor to the forage wagon", None),
            ("take it to the wagon", None), ("bring this to the wagon", None),
            ("bring it back to the wagon", None), ("hook it up to the wagon", None),
        ])

    def test_anything_else_is_not_understood(self):
        self.check([
            ("", None), ("   ", None), ("make me a sandwich", None), ("tractor", None),

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
        calls.clear()
        self.assertIsNone(chain.interpret("don't bring the tractor and wagon"))   # refused: model not asked
        self.assertEqual(calls, [])


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
