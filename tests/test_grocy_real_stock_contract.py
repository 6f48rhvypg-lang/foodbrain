import json
import os
from pathlib import Path
import unittest

from foodbrain_assistant.grocy_client import diagnose_stock_response


class GrocyRealStockContractTest(unittest.TestCase):
    def test_real_grocy_stock_payload_matches_parser_contract(self) -> None:
        payload_path = os.getenv("FOODBRAIN_GROCY_STOCK_JSON")
        if not payload_path:
            self.skipTest("Set FOODBRAIN_GROCY_STOCK_JSON to a local /api/stock export")

        with Path(payload_path).open("r", encoding="utf-8") as file:
            payload = json.load(file)

        diagnostics = diagnose_stock_response(payload)

        self.assertEqual(diagnostics["errors"], [], diagnostics)
        self.assertGreater(diagnostics["parsed_item_count"], 0, diagnostics)


if __name__ == "__main__":
    unittest.main()