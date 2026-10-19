"""Buying from the store: what to choose (companion/executor.py choose_store_item)
and a purchase verified on the farm (a new vehicle of that name, the money gone)."""
from dataclasses import replace
import unittest

from companion.executor import Executor, choose_store_item
from companion.language import RuleInterpreter
from protocol.model import Equipment, FarmInfo, StoreItem
from simulator.world import MockWorld


def item(i, name, categories, price, width=None, needed=None, power=None, brand=None):
    return StoreItem(i, name, brand, tuple(categories), price, True, width, needed, power)


CATALOG = (
    item(1, "Small Plow", ["PLOWS"], 9000, width=2.0, needed=60),
    item(2, "Big Plow", ["PLOWS"], 41000, width=6.5, needed=280),
    item(3, "Huge Plow", ["PLOWS"], 90000, width=9.0, needed=500),
    item(4, "2024 GMC Sierra Denali 2500 - 3500HD", ["CARS"], 90000, brand="GMC", power=401),
    item(5, "Gooseneck Trailer", ["LOWLOADERS"], 35000),
    # Live 2026-10-19 the first rules bought these two instead:
    item(8, "Removable GMC SRW Box", ["MISC"], 2000, brand="GMC"),            # a truck bed: no motor
    item(9, "Gooseneck Forestry", ["TRAILERS"], 12000, brand="LIZARD"),
    item(10, "1988-93 K-Series", ["CARS"], 20000, brand="GMC", power=210),
    item(6, "Series 9", ["TRACTORSL"], 300000, power=340),
    item(7, "MF 5700 S", ["TRACTORSM"], 90000, power=105),
)
TRACTORS = (Equipment("t-1", "tractor", "farm-a", (0.0, 0.0), name="Series 9"),
            Equipment("t-2", "tractor", "farm-a", (5.0, 0.0), name="MF 5700 S"))


class ChoiceTests(unittest.TestCase):
    def test_a_large_plow_is_the_widest_our_strongest_tractor_can_pull(self):
        self.assertEqual(choose_store_item(CATALOG, {"kind": "plow", "size": "large"}, 1e6, 340)[0].id, 2)
        self.assertEqual(choose_store_item(CATALOG, {"kind": "plow"}, 1e6, 340)[0].id, 1)          # cheapest
        self.assertEqual(choose_store_item(CATALOG, {"kind": "plow"}, 1e6, 40), (None, "NO_PLOW_FOR_OUR_TRACTORS"))
        self.assertEqual(choose_store_item(CATALOG, {"kind": "plow", "size": "large"}, 5000, 340),
                         (None, "NOT_ENOUGH_MONEY"))

    def test_named_vehicles_by_their_name(self):
        self.assertEqual(choose_store_item(CATALOG, {"kind": "truck", "name": "gmc"}, 1e6, None)[0].id, 4)
        self.assertEqual(choose_store_item(CATALOG, {"kind": "trailer", "name": "gooseneck"}, 1e6, None)[0].id, 5)
        self.assertEqual(choose_store_item(CATALOG, {"kind": "truck", "name": "lada"}, 1e6, None),
                         (None, "NOT_IN_STORE"))

    def test_the_words(self):
        rules = RuleInterpreter()
        self.assertEqual(rules.interpret("buy a large plow, the gmc truck and a gooseneck"),
                         {"version": 1, "command": "buy", "wants": [
                             {"kind": "plow", "size": "large"}, {"kind": "truck", "name": "gmc"},
                             {"kind": "trailer", "name": "gooseneck"}]})
        self.assertIsNone(rules.interpret("buy a large plow and a cow"))
        self.assertIsNone(rules.interpret("don't buy a plow"))


def buy(want, request_id="buy-1"):
    return {"version": 1, "request_id": request_id, "task": "buy", "farm_id": "farm-a",
            "target": "player_at_acceptance", "want": want}


class PurchaseTests(unittest.TestCase):
    def world(self, money=1e6, cls=MockWorld):
        world = cls(list(TRACTORS), {"farm-a": (0.0, 0.0)}, farm=FarmInfo(money, True, True, True))
        world.store_items = CATALOG
        world.mock_bought = Equipment("x", "implement", "farm-a", (500.0, 500.0), kinds=frozenset({"plow"}))
        return world

    def test_a_purchase_is_complete_with_the_new_vehicle_and_the_money_gone(self):
        world = self.world()
        result = Executor(world).execute(buy({"kind": "plow", "size": "large"}))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "BOUGHT"), result)
        self.assertEqual(result.states, ("FIND", "BUY", "VERIFY"))
        bought, = result.equipment_ids
        self.assertEqual(world.items[bought].name, "Big Plow")
        self.assertEqual(world.farm().money, 1e6 - 41000)

    def test_an_unseen_purchase_or_payment_is_never_complete(self):
        class Vanishing(MockWorld):
            def buy(self, item_id):
                return "BOUGHT"                                  # said bought, nothing appeared

        class Free(MockWorld):
            def buy(self, item_id):
                money = self.farm_info.money
                super().buy(item_id)
                self.farm_info = replace(self.farm_info, money=money)   # not charged

        for cls, reason in ((Vanishing, "PURCHASE_NOT_OBSERVED"), (Free, "PAYMENT_NOT_OBSERVED")):
            with self.subTest(world=cls.__name__):
                result = Executor(self.world(cls=cls)).execute(buy({"kind": "truck", "name": "gmc"}))
                self.assertEqual((result.status, result.reason), ("UNVERIFIED", reason))

    def test_nothing_is_bought_without_the_catalog_or_the_money(self):
        world = self.world()
        world.store_items = ()
        self.assertEqual(Executor(world).execute(buy({"kind": "plow"})).reason, "CATALOG_NOT_READY")
        world = self.world(money=100)
        result = Executor(world).execute(buy({"kind": "plow"}))
        self.assertEqual((result.status, result.reason), ("BLOCKED", "NOT_ENOUGH_MONEY"))
        self.assertEqual(len(world.items), 2)


if __name__ == "__main__":
    unittest.main()


class NamedPairTests(unittest.TestCase):
    """"bring the gooseneck to the farmhouse with the gmc": the towing vehicle and the
    implement by their names."""

    def test_the_words(self):
        rules = RuleInterpreter()
        self.assertEqual(rules.interpret("bring the gooseneck to the farmhouse with the gmc"),
                         {"version": 1, "command": "fetch_pair", "implement": "trailer",
                          "target": {"type": "place", "name": "farmhouse"},
                          "tractor_name": "gmc", "implement_name": "gooseneck"})
        self.assertNotIn("tractor_name", rules.interpret("bring the plow to the farmhouse"))
        self.assertIsNone(rules.interpret("bring the tractor with the gmc"))

    def test_only_the_named_vehicles_are_chosen(self):
        from companion.executor import named
        gmc = Equipment("g", "tractor", "farm-a", (0, 0), name="2024 GMC Sierra Denali 2500 - 3500HD")
        self.assertTrue(named(gmc, "gmc"))
        self.assertTrue(named(gmc, "gmc sierra"))
        self.assertFalse(named(gmc, "series 9"))
        self.assertTrue(named(gmc, None))
        self.assertFalse(named(replace(gmc, name=None), "gmc"))
