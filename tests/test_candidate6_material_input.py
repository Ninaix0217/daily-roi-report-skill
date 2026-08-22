from __future__ import annotations

import contextlib
import io
import json
import hashlib
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daily_roi_lib as roi  # noqa: E402
import daily_roi  # noqa: E402
import evidence_resolution as evidence  # noqa: E402


TARGET_DATE = "2026-08-18"
STORE = "STORE_A"
PRODUCT_A = "PRODUCT_A"
PRODUCT_B = "PRODUCT_B"
SKU_A = "100001"
GROSS_CENTS = 10000


def synthetic_template(*, products: tuple[str, ...] = (PRODUCT_A,), brush_values: dict[str, object] | None = None) -> dict:
    brush_values = brush_values or {}
    report_products = []
    sku_products = []
    sku_map = {}
    for offset, product in enumerate(products, 4):
        sku = str(100000 + offset - 3)
        report_products.append({
            "name": product,
            "row": offset,
            "cost_cell": f"C{offset}",
            "sales_cell": f"D{offset}",
            "roi_cell": f"E{offset}",
        })
        sku_products.append({
            "name": product,
            "row": offset,
            "single_sku": sku,
            "triple_sku": "",
            "gross_cell": f"F{offset}",
            "brush_cell": f"G{offset}",
            "real_cell": f"H{offset}",
            "brush_value": brush_values.get(product),
        })
        sku_map[sku] = {"product": product, "spec": "single"}
    return {
        "report": {
            "sheet": "REPORT",
            "template_date": TARGET_DATE,
            "date_cell": "A1",
            "total_cost_cell": "C8",
            "total_sales_cell": "D8",
            "total_roi_cell": "E8",
            "products": report_products,
        },
        "sku": {
            "sheet": "SKU",
            "map": sku_map,
            "products": sku_products,
            "conflicts": [],
            "secondary_value_range": None,
        },
        "identity": {"map": {}, "conflicts": []},
        "store_groups": [{"store": STORE, "products": list(products)}],
    }


def financial_book() -> dict:
    return {
        "sheets": [{
            "name": "FINANCE",
            "values": [
                ["流水单号", "投放账户名称", "投放日期", "交易类型", "支出"],
                ["TX-C6-SYNTHETIC", STORE, TARGET_DATE, "快车扣费", "0.00"],
            ],
        }],
    }


def sales_book(*, products: tuple[str, ...] = (PRODUCT_A,), amount_cents: int = GROSS_CENTS) -> dict:
    per_product = amount_cents // len(products)
    rows = [
        [TARGET_DATE, str(100001 + index), f"{per_product / 100:.2f}"]
        for index, _product in enumerate(products)
    ]
    rows[-1][2] = f"{(per_product + amount_cents - per_product * len(products)) / 100:.2f}"
    return {
        "sheets": [{
            "name": "SALES",
            "values": [
                ["日期", "SKU", "成交金额"],
                [TARGET_DATE, "合计", f"{amount_cents / 100:.2f}"],
                *rows,
            ],
        }],
    }


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review_cli(workspace: Path, reply: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    argv = ["daily_roi.py", "review", "--workspace", str(workspace), "--reply", reply]
    with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = daily_roi.main()
    return exit_code, stdout.getvalue(), stderr.getvalue()


class MaterialInputProductionHarness:
    def environment(
        self,
        root: Path,
        *,
        products: tuple[str, ...] = (PRODUCT_A,),
        brush_values: dict[str, object] | None = None,
    ):
        workspace = root / "workspace"
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        for name in ("template.xlsx", "finance.xlsx", "sales.xlsx"):
            (input_dir / name).write_bytes(f"synthetic-{name}".encode("ascii"))

        template = synthetic_template(products=products, brush_values=brush_values)
        bridge = MagicMock(name="candidate6_synthetic_workbook_bridge")
        bridge.inspect_template.side_effect = lambda path: deepcopy(template) if Path(path).name == "template.xlsx" else None
        bridge.inspect_xlsx.side_effect = lambda path: financial_book() if Path(path).name == "finance.xlsx" else sales_book(products=products)

        def fake_write(_template: Path, payload_path: Path, output: Path) -> dict:
            output.write_bytes(b"synthetic-written-workbook")
            bridge.written_payload = json.loads(payload_path.read_text(encoding="utf-8"))
            bridge.written_real_sales = {
                item["product"]: item["real_cents"]
                for item in bridge.written_payload.get("sales", {}).get("products", [])
            }
            bridge.roi_active = bool(bridge.written_payload.get("sales", {}).get("write"))
            return {"status": "PASS"}

        bridge.write.side_effect = fake_write
        bridge.verify.return_value = {"status": "PASS", "failures": []}
        dependencies = {"dependency_check": "PASS", "node": "node", "node_modules": "node_modules"}
        layout = {
            "sheets": [
                {"name": "REPORT", "merges": [], "cols": [], "row_heights": [], "cell_styles": []},
                {"name": "SKU", "merges": [], "cols": [], "row_heights": [], "cell_styles": []},
            ],
            "styles_sha256": None,
        }
        patches = (
            patch("daily_roi_lib.dependency_preflight", return_value=dependencies),
            patch("daily_roi_lib.WorkbookBridge", return_value=bridge),
            patch("daily_roi_lib.xlsx_layout_snapshot", return_value=layout),
        )
        return workspace, input_dir, output_dir, bridge, patches


class MissingBrushingProductionTests(unittest.TestCase):
    maxDiff = None

    def test_missing_brushing_blocks_output_and_persists_material_state(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = MaterialInputProductionHarness().environment(root)
            with patches[0], patches[1], patches[2]:
                state = roi.run_report(workspace, input_dir, output_dir)
                persisted = json.loads(roi.RuntimePaths.for_workspace(workspace).current_run.read_text(encoding="utf-8"))
                observed = {
                    "run_status": state["status"],
                    "human_gate_count": len(state["gates"]),
                    "cli_human_required_count": daily_roi.summary(state)["HUMAN_REQUIRED_COUNT"],
                    "workbook_write_count": bridge.write.call_count,
                    "output_exists": output_dir.exists(),
                    "sales_write": bridge.written_payload["sales"]["write"] if bridge.write.call_count else None,
                    "material_inputs": persisted.get("material_inputs"),
                }
        expected = {
            "run_status": "HUMAN_REQUIRED",
            "human_gate_count": 1,
            "cli_human_required_count": 1,
            "workbook_write_count": 0,
            "output_exists": False,
            "sales_write": None,
            "material_inputs": {
                "gross_sales": {"status": "KNOWN", "amount_cents": GROSS_CENTS},
                "brushing": {"status": "UNKNOWN", "amount_cents": None, "provenance": None},
                "real_sales": {"status": "UNRESOLVED", "amount_cents": None},
            },
        }
        self.assertEqual(observed, expected)

    def test_human_confirms_zero_resumes_same_run_and_writes_real_sales(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = MaterialInputProductionHarness().environment(root)
            with patches[0], patches[1], patches[2]:
                pending = roi.run_report(workspace, input_dir, output_dir)
                run_id = pending["run_id"]
                self.assertIn("未提供刷单数据", pending["review_ux"]["text"])
                self.assertEqual(pending["review_ux"]["recommended_reply"], "1是")
                exit_code, _stdout, stderr = review_cli(workspace, "1是")
                self.assertEqual(exit_code, 0, stderr)
                completed = json.loads(roi.RuntimePaths.for_workspace(workspace).current_run.read_text(encoding="utf-8"))
                confirmations = [
                    json.loads(line)
                    for line in roi.RuntimePaths.for_workspace(workspace).confirmations.read_text(encoding="utf-8").splitlines()
                ]

            self.assertEqual(completed["run_id"], run_id)
            self.assertEqual(completed["status"], "COMPLETE")
            self.assertEqual(completed["material_inputs"]["brushing"], {
                "status": "KNOWN_ZERO",
                "amount_cents": 0,
                "provenance": "HUMAN_CONFIRMED_ZERO",
            })
            self.assertEqual(completed["material_inputs"]["real_sales"], {"status": "KNOWN", "amount_cents": GROSS_CENTS})
            self.assertEqual(bridge.write.call_count, 1)
            self.assertEqual(bridge.written_real_sales[PRODUCT_A], GROSS_CENTS)
            self.assertTrue(bridge.roi_active)
            bridge.verify.assert_called_once()
            self.assertEqual(completed["verification"]["status"], "PASS")
            self.assertEqual(completed["audit"]["expense_difference_cents"], 0)
            self.assertEqual(completed["audit"]["sales"]["reported_cents"], completed["audit"]["sales"]["sku_sum_cents"])
            self.assertTrue(Path(completed["output_path"]).exists())
            self.assertFalse(roi.RuntimePaths.for_workspace(workspace).memory.exists())
            self.assertEqual(confirmations[0]["action"], "CONFIRM_ZERO")
            self.assertEqual(confirmations[0]["classification"], "RUN_ONLY")
            self.assertEqual(confirmations[0]["resolution"]["amount_cents"], 0)

    def test_human_nonzero_amount_resumes_single_product_run_with_cent_arithmetic(self):
        brushing_cents = 1234
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = MaterialInputProductionHarness().environment(root)
            with patches[0], patches[1], patches[2]:
                pending = roi.run_report(workspace, input_dir, output_dir)
                exit_code, _stdout, stderr = review_cli(workspace, "1改为12.34")
                self.assertEqual(exit_code, 0, stderr)
                completed = json.loads(roi.RuntimePaths.for_workspace(workspace).current_run.read_text(encoding="utf-8"))
                confirmations = [
                    json.loads(line)
                    for line in roi.RuntimePaths.for_workspace(workspace).confirmations.read_text(encoding="utf-8").splitlines()
                ]

            self.assertEqual(completed["run_id"], pending["run_id"])
            self.assertEqual(completed["material_inputs"]["brushing"], {
                "status": "KNOWN_AMOUNT",
                "amount_cents": brushing_cents,
                "provenance": "HUMAN_PROVIDED",
            })
            self.assertEqual(bridge.written_real_sales[PRODUCT_A], GROSS_CENTS - brushing_cents)
            self.assertEqual(bridge.write.call_count, 1)
            self.assertTrue(bridge.roi_active)
            bridge.verify.assert_called_once()
            self.assertEqual(completed["verification"]["status"], "PASS")
            self.assertEqual(completed["audit"]["expense_difference_cents"], 0)
            self.assertEqual(completed["audit"]["sales"]["reported_cents"], completed["audit"]["sales"]["sku_sum_cents"])
            self.assertTrue(Path(completed["output_path"]).exists())
            self.assertEqual(confirmations[0]["action"], "PROVIDE_AMOUNT")
            self.assertEqual(confirmations[0]["classification"], "RUN_ONLY")

    def test_source_zero_contract_is_not_assumed_and_prior_run_zero_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = MaterialInputProductionHarness().environment(
                root,
                brush_values={PRODUCT_A: 0},
            )
            with patches[0], patches[1], patches[2]:
                first = roi.run_report(workspace, input_dir, output_dir)
                completed = roi.resolve_review_batch(workspace, reply_text="1是")
                second = roi.run_report(workspace, input_dir, root / "second-output")

            self.assertEqual(first["status"], "HUMAN_REQUIRED")
            self.assertEqual(completed["status"], "COMPLETE")
            self.assertEqual(second["status"], "HUMAN_REQUIRED")
            self.assertEqual(second["material_inputs"]["brushing"]["status"], "UNKNOWN")
            self.assertEqual(bridge.write.call_count, 1)
            self.assertFalse(roi.RuntimePaths.for_workspace(workspace).memory.exists())

    def test_source_nonzero_brushing_preserves_existing_source_contract(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = MaterialInputProductionHarness().environment(
                root,
                brush_values={PRODUCT_A: "5.00"},
            )
            with patches[0], patches[1], patches[2]:
                completed = roi.run_report(workspace, input_dir, output_dir)

            self.assertEqual(completed["status"], "COMPLETE")
            self.assertEqual(completed["material_inputs"]["brushing"], {
                "status": "KNOWN_AMOUNT",
                "amount_cents": 500,
                "provenance": "SOURCE",
            })
            self.assertEqual(bridge.written_real_sales[PRODUCT_A], GROSS_CENTS - 500)
            self.assertEqual(bridge.write.call_count, 1)

    def test_multi_product_nonzero_aggregate_stays_blocked(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = MaterialInputProductionHarness().environment(
                root,
                products=(PRODUCT_A, PRODUCT_B),
            )
            with patches[0], patches[1], patches[2]:
                pending = roi.run_report(workspace, input_dir, output_dir)
                current_path = roi.RuntimePaths.for_workspace(workspace).current_run
                before = file_hash(current_path)
                with self.assertRaisesRegex(roi.DailyRoiError, "requires product-level allocation"):
                    roi.resolve_review_batch(workspace, reply_text="1改为12.34")

            self.assertEqual(file_hash(current_path), before)
            self.assertEqual(pending["status"], "HUMAN_REQUIRED")
            self.assertFalse(roi.RuntimePaths.for_workspace(workspace).confirmations.exists())
            self.assertFalse(roi.RuntimePaths.for_workspace(workspace).memory.exists())
            bridge.write.assert_not_called()

    def test_amount_domain_rejects_invalid_nonfinite_and_negative_values(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, _bridge, patches = MaterialInputProductionHarness().environment(root)
            with patches[0], patches[1], patches[2]:
                pending = roi.run_report(workspace, input_dir, output_dir)
            gate = pending["gates"][0]

        for invalid in ("INVALID_TEXT", "NaN", "Infinity", "-1.00", "100.01"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(roi.DailyRoiError):
                    roi.validate_human_gate_target(gate, invalid, pending["template_model"])


class MixedMaterialInputBatchTests(unittest.TestCase):
    @staticmethod
    def add_mixed_decisions(pending: dict, workspace: Path):
        review_decision = evidence.resolve_entity(
            "ALIAS_A",
            [PRODUCT_A, PRODUCT_B],
            entity_type="product",
            context_candidates=[PRODUCT_A, PRODUCT_B],
            cross_file_targets=[PRODUCT_A],
        )
        review_decision.update(
            mapping_sources=["ALIAS_A"],
            target_kind="product",
            business_relation_kind="PRODUCT_ASSIGNMENT",
        )
        review_batch = roi.make_review_batch([review_decision])

        store_decision = evidence.resolve_entity("FILE_A", [STORE, "STORE_B"], entity_type="store", context_candidates=[])
        store_decision.update(
            entity_type="campaign",
            sources=["FILE_A"],
            mapping_sources=["full_store_export"],
            target_kind="store",
            business_relation_kind="STORE_ALLOCATION",
            useful_hint={"evidence_status": "AMOUNT_ONLY_HINT", "candidate": STORE, "selected_answer": None},
        )
        store_gate = roi.resolution_gate(store_decision, "HG-06")

        product_decision = evidence.resolve_entity(
            "PLACEMENT_NEW",
            [PRODUCT_A, PRODUCT_B],
            entity_type="product",
            semantic_candidates=[],
        )
        product_decision.update(
            mapping_sources=["PLACEMENT_NEW"],
            target_kind="product",
            business_relation_kind="PRODUCT_ASSIGNMENT",
        )
        product_gate = roi.resolution_gate(product_decision, "HG-05")
        material_gate = next(
            gate for gate in pending["gates"]
            if (gate.get("candidate_resolution") or {}).get("business_relation_kind") == "MATERIAL_INPUT"
        )
        pending["review_batch"] = review_batch
        pending["gates"] = [store_gate, product_gate, material_gate]
        pending["status"] = "HUMAN_REQUIRED"
        pending["stage"] = "RESOLVE"
        paths = roi.RuntimePaths.for_workspace(workspace)
        roi.atomic_json(paths.current_run, pending)
        review_path = paths.runs_dir / pending["run_id"] / "review-batch.json"
        roi.atomic_json(review_path, review_batch)
        return paths, review_path

    @staticmethod
    def reply(pending: dict, amount_target: str) -> str:
        first_gate = max(int(item["number"]) for item in pending["review_batch"]["items"]) + 1
        return f"全部接受；{first_gate}是；{first_gate + 1}改为{PRODUCT_B}；{first_gate + 2}{amount_target}"

    def test_mixed_batch_with_brushing_zero_applies_atomically(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = MaterialInputProductionHarness().environment(
                root,
                products=(PRODUCT_A, PRODUCT_B),
            )
            with patches[0], patches[1], patches[2]:
                pending = roi.run_report(workspace, input_dir, output_dir)
                self.add_mixed_decisions(pending, workspace)
                exit_code, _stdout, stderr = review_cli(workspace, self.reply(pending, "是"))
                self.assertEqual(exit_code, 0, stderr)
                completed = json.loads(roi.RuntimePaths.for_workspace(workspace).current_run.read_text(encoding="utf-8"))
                confirmations = [
                    json.loads(line)
                    for line in roi.RuntimePaths.for_workspace(workspace).confirmations.read_text(encoding="utf-8").splitlines()
                ]

            self.assertEqual(completed["run_id"], pending["run_id"])
            self.assertEqual(completed["status"], "COMPLETE")
            self.assertEqual(bridge.write.call_count, 1)
            self.assertEqual({item["action"] for item in confirmations}, {"ACCEPT", "CONFIRM_HINT", "CORRECT", "CONFIRM_ZERO"})
            self.assertFalse(roi.RuntimePaths.for_workspace(workspace).memory.exists())
            self.assertFalse(completed["gates"])
            self.assertIsNone(completed["review_batch"])

    def test_invalid_amount_rejects_entire_mixed_batch_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = MaterialInputProductionHarness().environment(
                root,
                products=(PRODUCT_A, PRODUCT_B),
            )
            with patches[0], patches[1], patches[2]:
                pending = roi.run_report(workspace, input_dir, output_dir)
                paths, review_path = self.add_mixed_decisions(pending, workspace)
                current_before = file_hash(paths.current_run)
                review_before = file_hash(review_path)
                exit_code, _stdout, stderr = review_cli(workspace, self.reply(pending, "改为INVALID_TEXT"))
                self.assertEqual(exit_code, 1)
                self.assertIn("Invalid nonnegative monetary value", stderr)

            self.assertEqual(file_hash(paths.current_run), current_before)
            self.assertEqual(file_hash(review_path), review_before)
            self.assertFalse(paths.confirmations.exists())
            self.assertFalse(paths.memory.exists())
            bridge.write.assert_not_called()
            self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
