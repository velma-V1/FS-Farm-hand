import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_schema_is_valid(self):
        for name in ("intent.schema.json", "frame.schema.json"):
            with self.subTest(schema=name):
                schema = json.loads((ROOT / "protocol" / name).read_text())
                Draft202012Validator.check_schema(schema)
