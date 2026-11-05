"""Buying from the store: what to choose (companion/executor.py choose_store_item)
and a purchase verified on the farm (a new vehicle of that name, the money gone)."""
from dataclasses import replace
import unittest
import unittest.mock

from companion.executor import Executor, choose_store_item
from companion.language import RuleInterpreter
from protocol.model import Equipment, FarmInfo, OutputJoint, StoreItem
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
    item(11, "Pickup 2017", ["CARS"], 50000, power=600),
    item(12, "1124 Deckover Trailer", ["TRAILERS"], 28000, brand="BRANDT"),
)
THREE_POINT = (OutputJoint(1, "implement", 0.0, -2.0, True),)
TRACTORS = (Equipment("t-1", "tractor", "farm-a", (0.0, 0.0), name="Series 9", output_joints=THREE_POINT),
            Equipment("t-2", "tractor", "farm-a", (5.0, 0.0), name="MF 5700 S", output_joints=THREE_POINT),
            # Live: a pickup's power must not decide which plow the farm can pull.
            Equipment("p-1", "tractor", "farm-a", (9.0, 0.0), name="Pickup 2017",
                      output_joints=(OutputJoint(1, "trailer", 0.0, -2.5, True),)))


class ChoiceTests(unittest.TestCase):
    def test_a_large_plow_is_the_widest_our_strongest_tractor_can_pull(self):
        self.assertEqual(choose_store_item(CATALOG, {"kind": "plow", "size": "large"}, 1e6, 340)[0].id, 2)
        self.assertEqual(choose_store_item(CATALOG, {"kind": "plow"}, 1e6, 340)[0].id, 1)          # cheapest
        self.assertEqual(choose_store_item(CATALOG, {"kind": "plow"}, 1e6, 40), (None, "NO_PLOW_FOR_OUR_TRACTORS"))
        self.assertEqual(choose_store_item(CATALOG, {"kind": "plow", "size": "large"}, 5000, 340),
                         (None, "NOT_ENOUGH_MONEY"))

    def test_the_spending_limit_narrows_the_choice_and_says_when_nothing_fits(self):
        # AGENTS.md settings contract / ACCEPTANCE C11: the player's limit on one purchase.
        self.assertEqual(choose_store_item(CATALOG, {"kind": "plow", "size": "large"}, 1e6, 600, 50000)[0].id, 2)
        self.assertEqual(choose_store_item(CATALOG, {"kind": "plow", "size": "large"}, 1e6, 600, None)[0].id, 3)
        self.assertEqual(choose_store_item(CATALOG, {"kind": "plow"}, 1e6, 340, 9000)[0].id, 1)    # exactly at it
        self.assertEqual(choose_store_item(CATALOG, {"kind": "tractor", "name": "series 9"}, 1e6, None, 100000),
                         (None, "OVER_SPENDING_LIMIT"))
        # Money first: what the farm cannot pay is said so, whatever the limit.
        self.assertEqual(choose_store_item(CATALOG, {"kind": "plow"}, 5000, 340, 1000), (None, "NOT_ENOUGH_MONEY"))

    def test_a_named_item_is_matched_by_whole_words_never_by_pieces_of_them(self):
        # "9" was found inside "1988-93": "buy a series 9" chose the 20,000 K-Series pickup.
        self.assertEqual(choose_store_item(CATALOG, {"kind": "tractor", "name": "series 9"}, 1e6, None)[0].id, 6)
        self.assertEqual(choose_store_item(CATALOG, {"kind": "tractor", "name": "k-series"}, 1e6, None)[0].id, 10)
        self.assertEqual(choose_store_item(CATALOG, {"kind": "truck", "name": "gmc 3500"}, 1e6, None)[0].id, 4)
        self.assertEqual(choose_store_item(CATALOG, {"kind": "tractor", "name": "series 93"}, 1e6, None)[0].id, 10)
        self.assertEqual(choose_store_item(CATALOG, {"kind": "tractor", "name": "series 7"}, 1e6, None),
                         (None, "NOT_IN_STORE"))

    def test_a_named_item_the_farm_cannot_pay_is_refused_not_swapped_for_a_weaker_match(self):
        # The GMC Sierra is the truck the words name; at 60,000 it was swapped for the brand-only
        # K-Series the farm could pay. AGENTS.md: exact named equipment means that equipment.
        self.assertEqual(choose_store_item(CATALOG, {"kind": "truck", "name": "gmc"}, 60000, None),
                         (None, "NOT_ENOUGH_MONEY"))
        self.assertEqual(choose_store_item(CATALOG, {"kind": "truck", "name": "gmc"}, 1e6, None, 60000),
                         (None, "OVER_SPENDING_LIMIT"))
        self.assertEqual(choose_store_item(CATALOG, {"kind": "truck", "name": "gmc"}, 1e6, None)[0].id, 4)

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
        # The final acceptance request's rig (the user's completion plan).
        self.assertEqual(rules.interpret("buy the gmc truck and the 1124 deckover"),
                         {"version": 1, "command": "buy", "wants": [
                             {"kind": "truck", "name": "gmc"}, {"kind": "trailer", "name": "1124 deckover"}]})
        self.assertEqual(rules.interpret("buy a deckover trailer")["wants"], [{"kind": "trailer", "name": "deckover"}])
        self.assertEqual(choose_store_item(CATALOG, {"kind": "trailer", "name": "1124 deckover"}, 1e6, None)[0].id, 12)
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
            def buy(self, item_id, hitch=0, engine=0, options=()):
                return "BOUGHT"                                  # said bought, nothing appeared

        class Free(MockWorld):
            def buy(self, item_id, hitch=0, engine=0, options=()):
                money = self.farm_info.money
                super().buy(item_id, hitch, engine, options)
                self.farm_info = replace(self.farm_info, money=money)   # not charged

        for cls, reason in ((Vanishing, "PURCHASE_NOT_OBSERVED"), (Free, "PAYMENT_NOT_OBSERVED")):
            with self.subTest(world=cls.__name__):
                result = Executor(self.world(cls=cls)).execute(buy({"kind": "truck", "name": "gmc"}))
                self.assertEqual((result.status, result.reason), ("UNVERIFIED", reason))

    def test_nothing_is_bought_without_the_catalog_or_the_money(self):
        world = self.world()
        world.store_items = ()
        with unittest.mock.patch("companion.executor.CATALOG_WAIT_S", 0.0):
            self.assertEqual(Executor(world).execute(buy({"kind": "plow"})).reason, "CATALOG_NOT_READY")
        world = self.world(money=100)
        result = Executor(world).execute(buy({"kind": "plow"}))
        self.assertEqual((result.status, result.reason), ("BLOCKED", "NOT_ENOUGH_MONEY"))
        self.assertEqual(len(world.items), 3)

    def test_nothing_over_the_players_spending_limit_is_bought(self):
        world = self.world()
        world.spending_limit = 50000.0
        bought = []
        world.buy = lambda *args: bought.append(args)
        result = Executor(world).execute(buy({"kind": "truck", "name": "gmc"}))        # the 90,000 GMC
        self.assertEqual((result.status, result.reason), ("BLOCKED", "OVER_SPENDING_LIMIT"), result)
        self.assertEqual(bought, [])
        self.assertEqual(world.farm().money, 1e6)
        self.assertEqual(result.states, ("FIND",))

    def test_within_the_limit_the_purchase_goes_ahead(self):
        world = self.world()
        world.spending_limit = 50000.0
        result = Executor(world).execute(buy({"kind": "plow", "size": "large"}))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "BOUGHT"), result)
        self.assertEqual(world.items[result.equipment_ids[0]].name, "Big Plow")     # the widest within it
        for odd in (0, -1, "50", True, float("nan"), None):                           # no usable limit: none
            world = self.world()
            world.spending_limit = odd
            result = Executor(world).execute(buy({"kind": "plow", "size": "large"}))
            self.assertEqual(world.items[result.equipment_ids[0]].name, "Big Plow", odd)


class EngineOptionTests(unittest.TestCase):
    """The user's preferred tractor (2026-10-21): the Deutz-Fahr Series 9 with its
    "Boss Hoss" motor configuration (FS25_Deutz_Series_9_2019 series9.xml 1956-2023;
    GIANTS StoreItemUtil reads storeItem.configurations["motor"])."""

    SERIES9 = StoreItem(21, "Series 9 2019", "DEUTZFAHR", ("TRACTORSL",), 200000, True, None, None, 295,
                        engines=("9290 TTV", "9310 TTV", "9340 TTV", "9340 TTV Chipped", "1000 HP (Unrealistic)",
                                 "Boss Hoss"))
    WANT = {"kind": "tractor", "name": "deutz series 9", "engine": "boss hoss"}

    def test_the_words(self):
        rules = RuleInterpreter()
        for words in ("buy a Deutz-Farhr series 9 with a boss hoss engine setup",
                      "buy a deutz fahr series 9 tractor with the boss hoss engine",
                      "purchase a deutz-fahr series 9 with a boss hoss motor"):
            with self.subTest(words=words):
                self.assertEqual(rules.interpret(words), {"version": 1, "command": "buy", "wants": [self.WANT]})
        self.assertEqual(rules.interpret("buy a deutz series 9 tractor")["wants"],
                         [{"kind": "tractor", "name": "deutz series 9"}])

    def test_the_named_tractor_and_its_engine_option(self):
        from companion.executor import choose_engine
        catalog = CATALOG + (self.SERIES9,)
        self.assertEqual(choose_store_item(catalog, self.WANT, 1e6, None)[0].id, 21)
        self.assertEqual(choose_engine(self.SERIES9, self.WANT), 6)
        self.assertEqual(choose_engine(self.SERIES9, {"kind": "tractor", "name": "deutz"}), 0)       # default
        self.assertEqual(choose_engine(self.SERIES9, dict(self.WANT, engine="v12")), 0)

    def test_it_is_bought_with_that_engine_or_not_at_all(self):
        bought = []

        class Recording(MockWorld):
            def buy(self, item_id, hitch=0, engine=0, options=()):
                bought.append((item_id, hitch, engine))
                return super().buy(item_id, hitch, engine, options)

        for engines, expected in ((self.SERIES9.engines, ("COMPLETE", "BOUGHT")), ((), ("BLOCKED", "NO_ENGINE_OPTION"))):
            with self.subTest(engines=engines):
                bought.clear()
                world = Recording(list(TRACTORS), {"farm-a": (0.0, 0.0)}, farm=FarmInfo(1e6, True, True, True))
                world.store_items = CATALOG + (replace(self.SERIES9, engines=engines),)
                world.mock_bought = Equipment("x", "tractor", "farm-a", (500.0, 500.0))
                result = Executor(world).execute(buy(self.WANT))
                self.assertEqual((result.status, result.reason), expected, result)
                self.assertEqual(bought, [(21, 0, 6)] if expected[0] == "COMPLETE" else [])


class ConfigurationOptionTests(unittest.TestCase):
    """The user's 2026-10-21 plow: LIZARD's Titan Vineyard Plow (FS25_colossusPack) with its
    "25M Unrealistic" working-width set and its "25MPH / 41KPH" work speed, chosen by words."""

    TITAN = StoreItem(31, "Titan Vineyard Plow", "LIZARD", ("PLOWS",), 3000, True, None, 65, None,
                      options=(("config", "design16", 1, "11MPH / 18KPH"), ("config", "design16", 2, "15MPH / 25KPH"),
                               ("config", "design16", 3, "20MPH / 33KPH"), ("config", "design16", 4, "25MPH / 41KPH"),
                               ("set", None, 1, "Default"), ("set", None, 2, "1 Row"), ("set", None, 5, "25M Unrealistic"),
                               ("set", None, 6, "50M Unrealistic")))

    def test_the_words(self):
        rules = RuleInterpreter()
        for words in ("buy a lizard titan vineyard plow with the 25m unrealistic working width and the 25/mph speed selection",
                      "buy a 25m lizard titan vineyard plow with the 25 mph speed"):
            with self.subTest(words=words):
                want, = rules.interpret(words)["wants"]
                self.assertEqual((want["kind"], want["name"]), ("plow", "lizard titan vineyard"))
                self.assertEqual(sorted(want["options"]), sorted(["25m unrealistic" if "unrealistic" in words else "25m",
                                                                  "25 mph"]))

    def test_each_option_is_the_one_it_names(self):
        from companion.executor import choose_options
        self.assertEqual(choose_options(self.TITAN, {"options": ["25m unrealistic", "25 mph"]}), (7, 4))
        self.assertEqual(choose_options(self.TITAN, {"options": ["25m"]}), (7,))           # not 15MPH / 25KPH
        self.assertIsNone(choose_options(self.TITAN, {"options": ["100m"]}))
        self.assertIsNone(choose_options(self.TITAN, {"options": ["mph"]}))                # four of them
        self.assertEqual(choose_options(self.TITAN, {}), ())

    def test_bought_with_its_options_or_not_at_all(self):
        bought = []

        class Recording(MockWorld):
            def buy(self, item_id, hitch=0, engine=0, options=()):
                bought.append((item_id, tuple(options)))
                return super().buy(item_id, hitch, engine, options)

        for options, expected in ((["25m unrealistic", "25 mph"], ("COMPLETE", "BOUGHT")), (["75 mph"], ("BLOCKED", "NO_SUCH_OPTION"))):
            with self.subTest(options=options):
                bought.clear()
                world = Recording(list(TRACTORS), {"farm-a": (0.0, 0.0)}, farm=FarmInfo(1e6, True, True, True))
                world.store_items = CATALOG + (self.TITAN,)
                world.mock_bought = Equipment("x", "implement", "farm-a", (500.0, 500.0), kinds=frozenset({"plow"}))
                result = Executor(world).execute(buy({"kind": "plow", "name": "lizard titan vineyard", "options": options}))
                self.assertEqual((result.status, result.reason), expected, result)
                self.assertEqual(bought, [(31, (7, 4))] if expected[0] == "COMPLETE" else [])


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
        # By its brand too (live d14230aa9fb24d30-1: "the deutz" did not find "Series 9 2019").
        series9 = Equipment("d", "tractor", "farm-a", (0, 0), name="Series 9 2019", brand="DEUTZFAHR")
        self.assertTrue(named(series9, "deutz"))
        self.assertTrue(named(series9, "deutz series 9"))
        self.assertFalse(named(series9, "fendt"))
        self.assertFalse(named(replace(series9, brand=None), "deutz"))


class HitchOptionTests(unittest.TestCase):
    """Live 2026-10-20: the GMC Sierra defaults to "No Attachments"; pulling a gooseneck
    needs its "Gooseneck" attacherJoint configuration."""

    GMC = item(4, "2024 GMC Sierra Denali 2500 - 3500HD", ["CARS"], 90000, brand="GMC", power=401)
    GMC = StoreItem(**{**GMC.__dict__, "hitches": ("No Attachments", "Gooseneck", "Plow Mount", "Drop Hitch",
                                                   "Plow - Drop Hitch", "Plow - Gooseneck",
                                                   "Gooseneck - Drop Hitch", "All")})

    def test_the_gooseneck_option_that_keeps_a_plain_hitch(self):
        from companion.executor import choose_hitch
        self.assertEqual(choose_hitch(self.GMC, {"kind": "truck", "name": "gmc", "hitch": "gooseneck"}), 7)
        self.assertEqual(choose_hitch(self.GMC, {"kind": "truck", "name": "gmc"}), 0)          # default
        self.assertEqual(choose_hitch(replace(self.GMC, hitches=()), {"kind": "truck", "hitch": "gooseneck"}), 0)

    def test_a_truck_bought_with_a_gooseneck_gets_the_gooseneck_hitch(self):
        from companion import coordinator as coordination
        from protocol.model import Result
        sent = []

        class Scripted:
            def execute(self, intent, cancel=None, on_state=None, job=None):
                sent.append(intent["want"])
                return Result("COMPLETE", "BOUGHT", (f"n{len(sent)}",), ("FIND",))

        class World(MockWorld):
            cancel = None

            def retained(self):
                return ()

        c = coordination.Coordinator(World([], {}), Scripted(), lambda *a: None, lambda: "farm-a", lambda: "s")
        c.submit({"version": 1, "command": "buy", "wants": [
            {"kind": "truck", "name": "gmc"}, {"kind": "trailer", "name": "gooseneck"}]})
        c.wait(5)
        self.assertEqual(sent[0], {"kind": "truck", "name": "gmc", "hitch": "gooseneck"})
        sent.clear()
        c.submit({"version": 1, "command": "buy", "wants": [                 # a deckover is a gooseneck too
            {"kind": "truck", "name": "gmc"}, {"kind": "trailer", "name": "1124 deckover"}]})
        c.wait(5)
        self.assertEqual(sent[0], {"kind": "truck", "name": "gmc", "hitch": "gooseneck"})


class LandTests(unittest.TestCase):
    """"buy field 12" / "sell field 12": the field's farmland parcel through GIANTS' own request,
    complete only when the owner changed and the money moved (mock-only land and money)."""

    def world(self, owner="none", price=48000.0, money=100000.0):
        from protocol.model import FarmInfo, LandParcel
        from simulator.world import MockWorld
        world = MockWorld([], {"farm-a": (0.0, 0.0)}, farm=FarmInfo(money, True, True, True))
        world.mock_land = (LandParcel(11, 31, "other", 9000.0), LandParcel(12, 37, owner, price, 1.6))
        return world

    def land(self, action, field=12):
        from tests.test_farmhand import intent
        return {**intent(f"land-{action}-{field}"), "task": "land", "field": field, "land_action": action}

    def test_buying_and_selling_change_the_owner_and_move_the_money(self):
        from companion.executor import Executor
        world = self.world()
        result = Executor(world).execute(self.land("buy"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "LAND_BOUGHT"), result)
        self.assertEqual(result.states, ("FIND", "BUY_LAND", "VERIFY"))
        self.assertEqual(world.farm().money, 52000.0)
        self.assertEqual(next(p for p in world.land() if p.field == 12).owner, "mine")
        result = Executor(world).execute(self.land("sell"))
        self.assertEqual((result.status, result.reason), ("COMPLETE", "LAND_SOLD"), result)
        self.assertEqual(world.farm().money, 100000.0)

    def test_refusals_change_nothing(self):
        from companion.executor import Executor
        for world, action, field, reason in ((self.world(owner="mine"), "buy", 12, "LAND_ALREADY_OWNED"),
                                             (self.world(), "buy", 11, "LAND_NOT_FOR_SALE"),
                                             (self.world(money=1000.0), "buy", 12, "NOT_ENOUGH_MONEY"),
                                             (self.world(), "sell", 12, "LAND_NOT_OWNED"),
                                             (self.world(), "buy", 99, "FIELD_NOT_FOUND")):
            with self.subTest(reason=reason):
                money = world.farm().money
                result = Executor(world).execute(self.land(action, field))
                self.assertEqual((result.status, result.reason), ("BLOCKED", reason), result)
                self.assertEqual(world.farm().money, money)

    def test_land_over_the_spending_limit_is_not_bought_and_selling_is_never_limited(self):
        from companion.executor import Executor
        world = self.world()
        world.spending_limit = 40000.0                                           # the parcel costs 48,000
        result = Executor(world).execute(self.land("buy"))
        self.assertEqual((result.status, result.reason), ("BLOCKED", "OVER_SPENDING_LIMIT"), result)
        self.assertEqual(world.farm().money, 100000.0)
        self.assertEqual(next(p for p in world.land() if p.field == 12).owner, "none")
        world.spending_limit = 48000.0                                           # at the limit: bought
        self.assertEqual(Executor(world).execute(self.land("buy")).reason, "LAND_BOUGHT")
        world.spending_limit = 1.0
        self.assertEqual(Executor(world).execute(self.land("sell")).reason, "LAND_SOLD")

    def test_an_owner_change_without_the_money_is_not_complete(self):
        from companion.executor import Executor
        world = self.world()
        original = world.land_change

        def free(field, buy):
            money = world.farm_info
            out = original(field, buy)
            world.farm_info = money                  # owner changed, nothing charged
            return out
        world.land_change = free
        with unittest.mock.patch("companion.executor.Executor.LAND_VERIFY_S", 0.2):
            result = Executor(world).execute(self.land("buy"))
        self.assertEqual((result.status, result.reason), ("UNVERIFIED", "PAYMENT_NOT_OBSERVED"), result)
