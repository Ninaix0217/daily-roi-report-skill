from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_roi_lib import WorkbookBridge, convert_xls, find_financial_table, parse_campaign  # noqa: E402


class RealInputIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = os.environ.get("DAILY_ROI_GOLDEN_SOURCE")
        node = os.environ.get("DAILY_ROI_NODE")
        node_modules = os.environ.get("DAILY_ROI_NODE_MODULES")
        expected_date = os.environ.get("DAILY_ROI_EXPECTED_DATE")
        expected_financial = os.environ.get("DAILY_ROI_EXPECTED_FINANCIAL_CENTS")
        expected_campaign = os.environ.get("DAILY_ROI_EXPECTED_CAMPAIGN_CENTS")
        if not (source and node and node_modules and expected_date and expected_financial and expected_campaign):
            raise unittest.SkipTest("Private Golden paths are not configured")
        cls.source = Path(source)
        cls.bridge = WorkbookBridge(Path(node), Path(node_modules))
        cls.expected_date = expected_date
        cls.expected_financial = int(expected_financial)
        cls.expected_campaign = int(expected_campaign)

    def test_template_parsing_is_dynamic(self):
        template_path = next(path for path in self.source.glob("*.xlsx") if "每日综合投产登记" in path.name)
        model = self.bridge.inspect_template(template_path)
        self.assertEqual(model["observed"]["product_count"], len(model["report"]["products"]))
        self.assertEqual(model["observed"]["sku_count"], len(model["sku"]["map"]))
        self.assertEqual(model["report"]["template_date"], self.expected_date)

    def test_legacy_xls_conversion_and_financial_total(self):
        xls = next(self.source.glob("*.xls"))
        with tempfile.TemporaryDirectory() as root:
            converted = convert_xls(xls, Path(root))
            table = find_financial_table(self.bridge.inspect_xlsx(converted))
        self.assertIsNotNone(table)
        total = sum(item["expense_cents"] for item in table["records"] if item["date"] == self.expected_date and "快车扣费" in item["transaction_type"])
        self.assertEqual(total, self.expected_financial)

    def test_campaign_csv_schema_and_decimal_total(self):
        parsed_candidates = [parse_campaign(path) for path in self.source.glob("*.csv")]
        parsed = next(item for item in parsed_candidates if item and item["total_cents"] == self.expected_campaign)
        self.assertEqual(parsed["kind"], "campaign_regular")
        self.assertEqual(parsed["total_cents"], self.expected_campaign)
        self.assertEqual(parsed["dates"], [self.expected_date])


if __name__ == "__main__":
    unittest.main()
