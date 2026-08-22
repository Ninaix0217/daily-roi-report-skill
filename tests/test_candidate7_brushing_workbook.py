from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daily_roi_lib as roi  # noqa: E402


GROSS_CENTS = 10000
BRUSHING_CENTS = 1234
REAL_CENTS = 8766
PRODUCT = "PRODUCT_A"
PRODUCT_B = "PRODUCT_B"


def a1_indexes(address: str) -> tuple[int, int]:
    letters = "".join(char for char in address if char.isalpha())
    digits = "".join(char for char in address if char.isdigit())
    column = 0
    for char in letters.upper():
        column = column * 26 + ord(char) - 64
    return int(digits) - 1, column - 1


class Candidate7WorkbookHarness:
    def __init__(self, root: Path, *, source_brush: str = "missing", product_count: int = 1):
        self.workspace = root / "workspace"
        self.input_dir = root / "input"
        self.output_dir = root / "output"
        self.node = roi.find_node()
        self.node_modules = roi.find_node_modules()
        env = os.environ.copy()
        env["DAILY_ROI_NODE_MODULES"] = str(self.node_modules)
        subprocess.run(
            [
                str(self.node),
                str(ROOT / "tests" / "candidate7_workbook_fixture.mjs"),
                str(self.input_dir),
                source_brush,
                str(product_count),
            ],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.bridge = roi.WorkbookBridge(self.node, self.node_modules)
        self.write_count = 0

    @staticmethod
    def verify_without_render(bridge, template, output, payload, _render_dir):
        return bridge.call("verify", template, output, payload, "")

    @contextlib.contextmanager
    def production_worker_context(self):
        original_write = roi.WorkbookBridge.write

        def counted_write(bridge, template, payload, output):
            self.write_count += 1
            return original_write(bridge, template, payload, output)

        with patch.object(roi.WorkbookBridge, "write", counted_write), patch.object(
            roi.WorkbookBridge,
            "verify",
            self.verify_without_render,
        ):
            yield

    def run(self) -> dict:
        with self.production_worker_context():
            return roi.run_report(
                self.workspace,
                self.input_dir,
                self.output_dir,
                node=str(self.node),
                node_modules=str(self.node_modules),
            )

    def resolve(self, reply: str) -> dict:
        with self.production_worker_context():
            return roi.resolve_review_batch(
                self.workspace,
                reply_text=reply,
                node=str(self.node),
                node_modules=str(self.node_modules),
            )

    def reopened_sales(self, output_path: str, model: dict, product_name: str = PRODUCT) -> dict[str, object]:
        product = next(item for item in model["sku"]["products"] if item["name"] == product_name)
        book = self.bridge.inspect_xlsx(Path(output_path))
        sheet = next(item for item in book["sheets"] if item["name"] == model["sku"]["sheet"])

        def value(cell: str):
            row, col = a1_indexes(cell)
            return sheet["values"][row][col]

        return {
            "gross": value(product["gross_cell"]),
            "brush": value(product["brush_cell"]),
            "real": value(product["real_cell"]),
            "real_formula": sheet["formulas"][a1_indexes(product["real_cell"])[0]][a1_indexes(product["real_cell"])[1]],
        }


class Candidate7BrushingWorkbookTests(unittest.TestCase):
    maxDiff = None

    def test_human_nonzero_brushing_is_written_and_verified_through_production_worker(self):
        with tempfile.TemporaryDirectory() as temp_root:
            harness = Candidate7WorkbookHarness(Path(temp_root))
            pending = harness.run()
            self.assertEqual(pending["status"], "HUMAN_REQUIRED")
            self.assertEqual(pending["material_inputs"]["brushing"]["status"], "UNKNOWN")
            completed = harness.resolve("1改为12.34")
            payload = json.loads(Path(completed["payload_path"]).read_text(encoding="utf-8"))
            sales_entry = payload["sales"]["products"][0]
            reopened = harness.reopened_sales(completed["output_path"], completed["template_model"])

        observed = {
            "payload_brush_cents": sales_entry["brush_cents"],
            "payload_real_cents": sales_entry["real_cents"],
            "payload_brush_state": sales_entry["brush_business_state"],
            "payload_brush_provenance": sales_entry["brush_provenance"],
            "payload_brush_materialization": sales_entry["brush_materialization"],
            "gross_cents": round(float(reopened["gross"]) * 100),
            "brush_cents": None if reopened["brush"] is None else round(float(reopened["brush"]) * 100),
            "real_cents": round(float(reopened["real"]) * 100),
            "real_formula": reopened["real_formula"],
            "verification": completed["verification"],
            "run_status": completed["status"],
        }
        expected = {
            "payload_brush_cents": BRUSHING_CENTS,
            "payload_real_cents": REAL_CENTS,
            "payload_brush_state": "KNOWN_AMOUNT",
            "payload_brush_provenance": "HUMAN_PROVIDED",
            "payload_brush_materialization": "WRITE_AMOUNT",
            "gross_cents": GROSS_CENTS,
            "brush_cents": BRUSHING_CENTS,
            "real_cents": REAL_CENTS,
            "real_formula": "=ROUND(F2-G2,2)",
            "verification": {**completed["verification"], "status": "PASS", "failures": []},
            "run_status": "COMPLETE",
        }
        self.assertEqual(observed, expected)
        self.assertEqual(harness.write_count, 1)

    def test_human_confirmed_zero_is_explicit_and_real_sales_remains_gross(self):
        with tempfile.TemporaryDirectory() as temp_root:
            harness = Candidate7WorkbookHarness(Path(temp_root))
            pending = harness.run()
            completed = harness.resolve("1是")
            payload = json.loads(Path(completed["payload_path"]).read_text(encoding="utf-8"))
            reopened = harness.reopened_sales(completed["output_path"], completed["template_model"])

        self.assertEqual(completed["run_id"], pending["run_id"])
        self.assertEqual(completed["material_inputs"]["brushing"], {
            "status": "KNOWN_ZERO",
            "amount_cents": 0,
            "provenance": "HUMAN_CONFIRMED_ZERO",
        })
        self.assertEqual(payload["sales"]["products"][0]["brush_business_state"], "KNOWN_ZERO")
        self.assertEqual(payload["sales"]["products"][0]["brush_provenance"], "HUMAN_CONFIRMED_ZERO")
        self.assertEqual(payload["sales"]["products"][0]["brush_materialization"], "WRITE_ZERO")
        self.assertEqual(round(float(reopened["brush"]) * 100), 0)
        self.assertEqual(round(float(reopened["real"]) * 100), GROSS_CENTS)
        self.assertEqual(completed["verification"]["status"], "PASS")
        self.assertEqual(completed["status"], "COMPLETE")
        self.assertEqual(harness.write_count, 1)

    def test_source_nonzero_brushing_remains_written_and_verified(self):
        with tempfile.TemporaryDirectory() as temp_root:
            harness = Candidate7WorkbookHarness(Path(temp_root), source_brush="5.00")
            completed = harness.run()
            payload = json.loads(Path(completed["payload_path"]).read_text(encoding="utf-8"))
            reopened = harness.reopened_sales(completed["output_path"], completed["template_model"])

        self.assertEqual(completed["material_inputs"]["brushing"], {
            "status": "KNOWN_AMOUNT",
            "amount_cents": 500,
            "provenance": "SOURCE",
        })
        self.assertEqual(round(float(reopened["brush"]) * 100), 500)
        self.assertEqual(payload["sales"]["products"][0]["brush_business_state"], "KNOWN_AMOUNT")
        self.assertEqual(payload["sales"]["products"][0]["brush_provenance"], "SOURCE")
        self.assertEqual(payload["sales"]["products"][0]["brush_materialization"], "WRITE_AMOUNT")
        self.assertEqual(round(float(reopened["real"]) * 100), GROSS_CENTS - 500)
        self.assertEqual(completed["verification"]["status"], "PASS")
        self.assertEqual(completed["status"], "COMPLETE")
        self.assertEqual(harness.write_count, 1)

    def test_source_nonzero_preserves_implicit_zero_product_blank(self):
        with tempfile.TemporaryDirectory() as temp_root:
            harness = Candidate7WorkbookHarness(Path(temp_root), source_brush="12.34", product_count=2)
            completed = harness.run()
            payload = json.loads(Path(completed["payload_path"]).read_text(encoding="utf-8"))
            entries = {item["product"]: item for item in payload["sales"]["products"]}
            reopened_a = harness.reopened_sales(completed["output_path"], completed["template_model"], PRODUCT)
            reopened_b = harness.reopened_sales(completed["output_path"], completed["template_model"], PRODUCT_B)

            strict_payload = json.loads(json.dumps(payload))
            strict_b = next(item for item in strict_payload["sales"]["products"] if item["product"] == PRODUCT_B)
            strict_b.update({
                "brush_business_state": "KNOWN_ZERO",
                "brush_provenance": "HUMAN_CONFIRMED_ZERO",
                "brush_materialization": "WRITE_ZERO",
            })
            strict_payload_path = Path(temp_root) / "strict-write-zero-payload.json"
            strict_payload_path.write_text(json.dumps(strict_payload), encoding="utf-8")
            strict_verification = harness.bridge.call(
                "verify",
                Path(completed["classification"]["template"]),
                Path(completed["output_path"]),
                strict_payload_path,
                "",
            )

        self.assertEqual(round(float(reopened_a["brush"]) * 100), BRUSHING_CENTS)
        self.assertIsNone(reopened_b["brush"])
        self.assertEqual(entries[PRODUCT]["brush_business_state"], "KNOWN_AMOUNT")
        self.assertEqual(entries[PRODUCT]["brush_provenance"], "SOURCE")
        self.assertEqual(entries[PRODUCT]["brush_materialization"], "WRITE_AMOUNT")
        self.assertEqual(entries[PRODUCT_B]["brush_business_state"], "KNOWN_ZERO")
        self.assertEqual(entries[PRODUCT_B]["brush_provenance"], "DERIVED_NO_EXPLICIT_PRODUCT_FACT")
        self.assertEqual(entries[PRODUCT_B]["brush_materialization"], "PRESERVE")
        self.assertEqual(round(float(reopened_a["real"]) * 100), 5000 - BRUSHING_CENTS)
        self.assertEqual(round(float(reopened_b["real"]) * 100), 5000)
        self.assertEqual(completed["verification"]["status"], "PASS")
        self.assertEqual(strict_verification["status"], "FAIL")
        self.assertIn(f"brush_representation:{PRODUCT_B}:missing", strict_verification["failures"])
        self.assertEqual(completed["status"], "COMPLETE")
        self.assertEqual(harness.write_count, 1)


if __name__ == "__main__":
    unittest.main()
