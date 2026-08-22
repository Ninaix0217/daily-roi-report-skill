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


def a1_indexes(address: str) -> tuple[int, int]:
    letters = "".join(char for char in address if char.isalpha())
    digits = "".join(char for char in address if char.isdigit())
    column = 0
    for char in letters.upper():
        column = column * 26 + ord(char) - 64
    return int(digits) - 1, column - 1


class Candidate7WorkbookHarness:
    def __init__(self, root: Path, *, source_brush: str = "missing"):
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

    def reopened_sales(self, output_path: str, model: dict) -> dict[str, object]:
        product = next(item for item in model["sku"]["products"] if item["name"] == PRODUCT)
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
            reopened = harness.reopened_sales(completed["output_path"], completed["template_model"])

        self.assertEqual(completed["run_id"], pending["run_id"])
        self.assertEqual(round(float(reopened["brush"]) * 100), 0)
        self.assertEqual(round(float(reopened["real"]) * 100), GROSS_CENTS)
        self.assertEqual(completed["verification"]["status"], "PASS")
        self.assertEqual(completed["status"], "COMPLETE")
        self.assertEqual(harness.write_count, 1)

    def test_source_nonzero_brushing_remains_written_and_verified(self):
        with tempfile.TemporaryDirectory() as temp_root:
            harness = Candidate7WorkbookHarness(Path(temp_root), source_brush="5.00")
            completed = harness.run()
            reopened = harness.reopened_sales(completed["output_path"], completed["template_model"])

        self.assertEqual(completed["material_inputs"]["brushing"], {
            "status": "KNOWN_AMOUNT",
            "amount_cents": 500,
            "provenance": "SOURCE",
        })
        self.assertEqual(round(float(reopened["brush"]) * 100), 500)
        self.assertEqual(round(float(reopened["real"]) * 100), GROSS_CENTS - 500)
        self.assertEqual(completed["verification"]["status"], "PASS")
        self.assertEqual(completed["status"], "COMPLETE")
        self.assertEqual(harness.write_count, 1)


if __name__ == "__main__":
    unittest.main()
