from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daily_roi_lib as roi  # noqa: E402
import evidence_resolution as evidence  # noqa: E402


TARGET_DATE = "2026-08-18"
STORE = "STORE_A"
PRODUCT_A = "PRODUCT_A"
PRODUCT_B = "PRODUCT_B"
PLACEMENT = "PLACEMENT_NEW_001"
AMBIGUOUS_LABEL = "PRODUCT"
AMOUNT_CENTS = 12345


def synthetic_template(*, trusted_placement: bool = False, conflicting_placement: bool = False) -> dict:
    template = {
        "report": {
            "sheet": "REPORT",
            "template_date": TARGET_DATE,
            "date_cell": "A1",
            "total_cost_cell": "C8",
            "total_sales_cell": "D8",
            "total_roi_cell": "E8",
            "products": [
                {"name": PRODUCT_A, "row": 4, "cost_cell": "C4", "sales_cell": "D4", "roi_cell": "E4"},
                {"name": PRODUCT_B, "row": 5, "cost_cell": "C5", "sales_cell": "D5", "roi_cell": "E5"},
            ],
        },
        "sku": None,
        "identity": {"map": {}, "conflicts": []},
        "store_groups": [{"store": STORE, "products": [PRODUCT_A, PRODUCT_B]}],
    }
    if trusted_placement:
        template["identity"]["map"] = {
            "placement_id": {PLACEMENT: {"product": PRODUCT_A}}
        }
    if conflicting_placement:
        template["identity"]["conflicts"] = [{
            "identity_type": "placement_id",
            "value": PLACEMENT,
            "products": [PRODUCT_A, PRODUCT_B],
        }]
    return template


def synthetic_financial_book() -> dict:
    return {
        "sheets": [{
            "name": "FINANCE",
            "values": [
                ["流水单号", "投放账户名称", "投放日期", "交易类型", "支出"],
                ["TX-C4-SYNTHETIC", STORE, TARGET_DATE, "快车扣费", "123.45"],
            ],
        }],
    }


class IdentityProvenanceProductionTests(unittest.TestCase):
    def _environment(
        self,
        root: Path,
        *,
        trusted_placement: bool = False,
        conflicting_placement: bool = False,
        ambiguous_costs: tuple[str, ...] = ("123.45",),
    ):
        workspace = root / "workspace"
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        (input_dir / "template.xlsx").write_bytes(b"synthetic-template-placeholder")
        (input_dir / "finance.xlsx").write_bytes(b"synthetic-financial-placeholder")
        ambiguous_rows = "".join(
            f"PLAN_AMBIGUOUS_{index},{PLACEMENT},{AMBIGUOUS_LABEL},{cost},{TARGET_DATE}\n"
            for index, cost in enumerate(ambiguous_costs, 1)
        )
        (input_dir / "placement.csv").write_text(
            "计划名称,投放ID,商品名称,花费,日期\n"
            f"PLAN_SEED,{PLACEMENT},{PRODUCT_A},0.00,{TARGET_DATE}\n"
            f"{ambiguous_rows}",
            encoding="utf-8-sig",
        )

        bridge = MagicMock(name="candidate4_synthetic_workbook_bridge")
        bridge.inspect_template.side_effect = lambda path: (
            deepcopy(synthetic_template(
                trusted_placement=trusted_placement,
                conflicting_placement=conflicting_placement,
            ))
            if Path(path).name == "template.xlsx"
            else None
        )
        bridge.inspect_xlsx.return_value = synthetic_financial_book()

        def fake_write(_template: Path, payload_path: Path, output: Path) -> dict:
            output.write_bytes(b"synthetic-written-workbook")
            bridge.written_payload = json.loads(payload_path.read_text(encoding="utf-8"))
            bridge.written_cost_formulas = {
                item["product"]: "=" + "+".join(
                    f"{cents / 100:.2f}" for cents in (item["components_cents"] or [0])
                )
                for item in bridge.written_payload["product_expenses"]
            }
            return {"status": "PASS"}

        bridge.write.side_effect = fake_write
        bridge.verify.return_value = {"status": "PASS", "failures": []}
        dependencies = {"dependency_check": "PASS", "node": "node", "node_modules": "node_modules"}
        layout = {
            "sheets": [{"name": "REPORT", "merges": [], "cols": [], "row_heights": [], "cell_styles": []}],
            "styles_sha256": None,
        }
        patches = (
            patch("daily_roi_lib.dependency_preflight", return_value=dependencies),
            patch("daily_roi_lib.WorkbookBridge", return_value=bridge),
            patch("daily_roi_lib.xlsx_layout_snapshot", return_value=layout),
        )
        return workspace, input_dir, output_dir, bridge, patches

    @staticmethod
    def _ambiguous_resolutions(state: dict) -> list[dict]:
        return [
            item
            for item in state["audit"]["resolutions"]
            if item.get("source") == AMBIGUOUS_LABEL
        ]

    @classmethod
    def _ambiguous_resolution(cls, state: dict) -> dict:
        matching = cls._ambiguous_resolutions(state)
        if len(matching) != 1:
            raise AssertionError(f"expected one ambiguous placement resolution, got {matching!r}")
        return matching[0]

    def test_unseen_current_file_placement_binding_is_not_hard_identity(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = self._environment(root)
            with patches[0], patches[1], patches[2]:
                state = roi.run_report(workspace, input_dir, output_dir)

        decision = self._ambiguous_resolution(state)
        self.assertNotEqual(decision["decision"], evidence.VERIFIED)
        self.assertNotIn(evidence.HARD_IDENTITY, decision.get("evidence_classes", []))
        derived = [
            proof for proof in decision.get("evidence", [])
            if proof.get("origin") == "current_file_exact_identity_bridge"
        ]
        self.assertTrue(derived)
        self.assertTrue(all(proof.get("binding_trust") == "DERIVED" for proof in derived))
        bridge.write.assert_not_called()
        self.assertIsNone(state["output_path"])

    def test_independent_template_placement_binding_remains_hard_identity(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, _bridge, patches = self._environment(
                root,
                trusted_placement=True,
            )
            with patches[0], patches[1], patches[2]:
                state = roi.run_report(workspace, input_dir, output_dir)

        decision = self._ambiguous_resolution(state)
        self.assertEqual(decision["decision"], evidence.VERIFIED)
        self.assertIn(evidence.HARD_IDENTITY, decision["evidence_classes"])
        trusted = [
            proof for proof in decision["evidence"]
            if proof.get("origin") == "template_identity_map"
        ]
        self.assertTrue(trusted)
        self.assertTrue(all(proof.get("binding_trust") == "HARD" for proof in trusted))
        self.assertTrue(all(proof.get("independent_hard_binding") is True for proof in trusted))

    def test_same_file_repetition_and_sibling_records_do_not_upgrade_binding_trust(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = self._environment(
                root,
                ambiguous_costs=("40.00", "40.00", "43.45"),
            )
            with patches[0], patches[1], patches[2]:
                state = roi.run_report(workspace, input_dir, output_dir)

        decisions = self._ambiguous_resolutions(state)
        self.assertEqual(len(decisions), 3)
        self.assertTrue(all(item["decision"] != evidence.VERIFIED for item in decisions))
        self.assertTrue(all(evidence.HARD_IDENTITY not in item["evidence_classes"] for item in decisions))
        derived = [
            proof
            for decision in decisions
            for proof in decision["evidence"]
            if proof.get("type") == "current_run_derived_identity_binding"
        ]
        self.assertEqual(len({proof.get("lineage_id") for proof in derived}), 1)
        self.assertTrue(all(proof.get("independent_hard_binding") is False for proof in derived))
        product_gate = next(
            gate for gate in state["gates"]
            if ((gate.get("candidate_resolution") or {}).get("entity_type") == "product")
        )
        self.assertEqual(len(product_gate["evidence"]["occurrences"]), 3)
        bridge.write.assert_not_called()

    def test_conflicting_trusted_placement_bindings_still_block(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = self._environment(
                root,
                conflicting_placement=True,
            )
            with patches[0], patches[1], patches[2]:
                state = roi.run_report(workspace, input_dir, output_dir)

        decision = self._ambiguous_resolution(state)
        self.assertEqual(decision["decision"], evidence.HUMAN_REQUIRED)
        self.assertIn("conflicting_exact_targets", {item["type"] for item in decision["contradictions"]})
        bridge.write.assert_not_called()

    def test_human_correction_controls_final_product_allocation_and_formula(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            workspace, input_dir, output_dir, bridge, patches = self._environment(root)
            with patches[0], patches[1], patches[2]:
                pending = roi.run_report(workspace, input_dir, output_dir)
                run_id = pending["run_id"]
                product_gate = next(
                    gate for gate in pending["gates"]
                    if ((gate.get("candidate_resolution") or {}).get("entity_type") == "product")
                )
                after_correction = roi.resolve_gate(
                    workspace,
                    product_gate["gate_id"],
                    target=PRODUCT_B,
                    persistence="RUN_ONLY",
                )
                self.assertEqual(after_correction["run_id"], run_id)
                self.assertEqual(bridge.write.call_count, 0)
                completed = roi.resolve_review_batch(workspace, accept_all=True)

                confirmations = [
                    json.loads(line)
                    for line in roi.RuntimePaths.for_workspace(workspace).confirmations.read_text(encoding="utf-8").splitlines()
                ]

        self.assertEqual(completed["run_id"], run_id)
        self.assertEqual(completed["status"], "COMPLETE")
        self.assertEqual(bridge.write.call_count, 1)
        expenses = {
            item["product"]: item
            for item in bridge.written_payload["product_expenses"]
        }
        self.assertEqual(expenses[PRODUCT_A]["components_cents"], [])
        self.assertEqual(expenses[PRODUCT_A]["total_cents"], 0)
        self.assertEqual(expenses[PRODUCT_B]["components_cents"], [AMOUNT_CENTS])
        self.assertEqual(expenses[PRODUCT_B]["total_cents"], AMOUNT_CENTS)
        self.assertNotIn("123.45", bridge.written_cost_formulas[PRODUCT_A])
        self.assertIn("123.45", bridge.written_cost_formulas[PRODUCT_B])
        self.assertEqual(bridge.written_payload["expense_total_cents"], AMOUNT_CENTS)
        self.assertEqual(completed["audit"]["expense_difference_cents"], 0)
        correction = next(item for item in confirmations if item.get("gate_id") == product_gate["gate_id"])
        self.assertEqual(correction["resolution"]["target"], PRODUCT_B)
        self.assertEqual(correction["classification"], "RUN_ONLY")


if __name__ == "__main__":
    unittest.main()
