import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from lupa.lua54 import LuaRuntime

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_schema_is_valid(self):
        for name in ("intent.schema.json", "frame.schema.json"):
            with self.subTest(schema=name):
                schema = json.loads((ROOT / "protocol" / name).read_text())
                Draft202012Validator.check_schema(schema)

    def test_lua_boundary(self):
        lua = LuaRuntime(unpack_returned_tuples=True)
        boundary = lua.execute((ROOT / "mod/FarmhandBoundary.lua").read_text())
        self.assertFalse(boundary.capabilities().move)
        self.assertFalse(boundary.capabilities().attach)
        self.assertFalse(boundary.capabilities().stop)
        for operation in ("move", "attach", "stop", "unknown"):
            result = boundary.execute(operation)
            self.assertFalse(result.ok)
            self.assertEqual(result.code, "UNSUPPORTED")
        self.assertFalse(boundary.execute(None).ok)
