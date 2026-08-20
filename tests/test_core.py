from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_roi_lib import (  # noqa: E402
    DailyRoiError,
    LocalMemory,
    RuntimePaths,
    WORKFLOW_RULE_STALE_TEMPLATE,
    atomic_json,
    build_golden_payload,
    cents_text,
    classify_duplicate_records,
    decimal_money,
    dependency_preflight,
    evaluate_dates,
    make_gate,
    resolve_gate,
    resolve_product,
    to_cents,
)


def minimal_template(products: list[str]) -> dict:
    rows = []
    for index, product in enumerate(products, 4):
        rows.append({"name": product, "row": index, "cost_cell": f"C{index}", "sales_cell": f"D{index}", "roi_cell": f"E{index}"})
    return {
        "report": {"products": rows, "template_date": "2026-08-18"},
        "sku": None,
        "store_groups": [],
    }


def financial_record(store: str, cents: int, target_date: str = "2026-08-18") -> dict:
    return {
        "source_name": "finance.xls",
        "records": [{
            "row": 2,
            "transaction_id": "tx-1",
            "store": store,
            "date": target_date,
            "transaction_type": "快车扣费",
            "expense_cents": cents,
        }],
    }


class MoneyTests(unittest.TestCase):
    def test_decimal_money_preserves_cents(self):
        self.assertEqual(decimal_money("703.50"), decimal_money("703.50"))
        self.assertEqual(to_cents("703.50"), 70350)
        self.assertEqual(cents_text(70350), "703.50")

    def test_binary_float_tail_is_normalized_only_at_currency_boundary(self):
        self.assertEqual(to_cents("0.1"), 10)
        self.assertEqual(to_cents("0.2"), 20)
        self.assertEqual(to_cents("0.30000000000000004"), 30)


class MemoryTests(unittest.TestCase):
    def test_memory_rejects_unconfirmed_inference(self):
        with self.assertRaises(DailyRoiError):
            LocalMemory.validate({
                "schema_version": 1,
                "entity_mappings": [{"kind": "entity_mapping", "entity_type": "product", "source": "A", "target": "B", "status": "candidate", "source_type": "ai_inference"}],
                "workflow_rules": [],
            })

    def test_memory_is_workspace_scoped(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            memory_a = LocalMemory(RuntimePaths.for_workspace(Path(a)))
            memory_b = LocalMemory(RuntimePaths.for_workspace(Path(b)))
            memory_a.add_mapping("product", "来源产品", "标准产品", gate_id="HG-A")
            self.assertEqual(LocalMemory(RuntimePaths.for_workspace(Path(a))).resolve("product", "来源产品"), "标准产品")
            self.assertIsNone(memory_b.resolve("product", "来源产品"))

    def test_unknown_mapping_confirm_and_next_run_reuse(self):
        with tempfile.TemporaryDirectory() as root:
            paths = RuntimePaths.for_workspace(Path(root))
            memory = LocalMemory(paths)
            template = minimal_template(["标准产品"])
            payload, audit, gates = build_golden_payload(
                template,
                financial_record("示例店铺-来源产品", 1500),
                [],
                None,
                memory,
                {},
                "2026-08-18",
            )
            self.assertTrue(any(gate["gate_type"] == "HG-01" for gate in gates))
            memory.add_mapping("product", "来源产品", "标准产品", gate_id="HG-A")
            payload, audit, gates = build_golden_payload(
                template,
                financial_record("示例店铺-来源产品", 1500),
                [],
                None,
                LocalMemory(paths),
                {},
                "2026-08-18",
            )
            self.assertFalse(gates)
            self.assertEqual(payload["expense_total_cents"], 1500)
            self.assertEqual(audit["expense_difference_cents"], 0)

    def test_resolve_persists_and_resumes_current_run(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = RuntimePaths.for_workspace(workspace)
            gate = make_gate(
                "HG-01",
                "unknown",
                {"source": "来源产品"},
                "confirm",
                candidate={"entity_type": "product", "source": "来源产品", "target": "标准产品"},
                persistence={"entity_type": "product", "source": "来源产品", "target": "标准产品"},
            )
            state = {
                "schema_version": 1,
                "run_id": "run-1",
                "gates": [gate],
                "run_mappings": {},
                "input_dir": str(workspace / "input"),
                "output_dir": str(workspace / "output"),
            }
            atomic_json(paths.current_run, state)
            with patch("daily_roi_lib.run_report", return_value={"status": "COMPLETE"}) as resumed:
                result = resolve_gate(workspace, gate["gate_id"], target=None, persistence="PERSISTENT_REUSABLE")
            self.assertEqual(result["status"], "COMPLETE")
            self.assertEqual(LocalMemory(paths).resolve("product", "来源产品"), "标准产品")
            self.assertTrue(resumed.called)


class DependencyPreflightTests(unittest.TestCase):
    def test_legacy_xls_fails_clearly_when_libreoffice_is_missing(self):
        with tempfile.TemporaryDirectory() as root:
            input_dir = Path(root)
            (input_dir / "legacy.xls").write_bytes(b"fixture")
            with patch("daily_roi_lib.find_node", return_value=Path("node")), patch(
                "daily_roi_lib.find_node_modules", return_value=Path("node_modules")
            ), patch(
                "daily_roi_lib.find_soffice",
                side_effect=DailyRoiError("DEPENDENCY_CHECK=FAIL\nMISSING=LibreOffice"),
            ):
                with self.assertRaisesRegex(DailyRoiError, "MISSING=LibreOffice"):
                    dependency_preflight(input_dir)

    def test_xlsx_csv_inputs_do_not_require_libreoffice(self):
        with tempfile.TemporaryDirectory() as root:
            input_dir = Path(root)
            (input_dir / "template.xlsx").write_bytes(b"fixture")
            with patch("daily_roi_lib.find_node", return_value=Path("node")), patch(
                "daily_roi_lib.find_node_modules", return_value=Path("node_modules")
            ), patch("daily_roi_lib.find_soffice") as soffice:
                result = dependency_preflight(input_dir)
            self.assertEqual(result["dependency_check"], "PASS")
            self.assertEqual(result["libreoffice"], "NOT_REQUIRED")
            soffice.assert_not_called()


class DateTests(unittest.TestCase):
    def test_stale_template_rule_auto_updates_only_template(self):
        with tempfile.TemporaryDirectory() as root:
            memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
            memory.add_rule(WORKFLOW_RULE_STALE_TEMPLATE, gate_id="HG-DATE")
            target, update, gates = evaluate_dates({"financial": ["2026-08-17"], "sales": ["2026-08-17"], "campaign": ["2026-08-17"]}, "2026-08-16", memory)
            self.assertEqual(target, "2026-08-17")
            self.assertTrue(update)
            self.assertFalse(gates)

    def test_source_date_conflict_is_not_bypassed_by_rule(self):
        with tempfile.TemporaryDirectory() as root:
            memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
            memory.add_rule(WORKFLOW_RULE_STALE_TEMPLATE, gate_id="HG-DATE")
            _, update, gates = evaluate_dates({"financial": ["2026-08-18"], "sales": ["2026-08-18"], "campaign": ["2026-08-17"]}, "2026-08-16", memory)
            self.assertFalse(update)
            self.assertEqual([gate["gate_type"] for gate in gates], ["HG-02"])


class DedupTests(unittest.TestCase):
    def test_same_identity_and_full_record_is_proven_duplicate(self):
        row = {"source_kind": "campaign", "record_id": "id-1", "raw": {"ID": "id-1", "花费": "10.00"}, "resolved_product": "A", "cost_cents": 1000, "plan": "P", "date": "2026-08-18"}
        proven, suspected = classify_duplicate_records([row, dict(row)])
        self.assertEqual(len(proven), 1)
        self.assertFalse(suspected)

    def test_similarity_without_identity_requires_human(self):
        first = {"source_kind": "regular", "record_id": "", "raw": {"plan": "A1"}, "resolved_product": "A", "cost_cents": 1000, "plan": "A1", "date": "2026-08-18"}
        second = {"source_kind": "full", "record_id": "", "raw": {"plan": "A3"}, "resolved_product": "A", "cost_cents": 1000, "plan": "A3", "date": "2026-08-18"}
        # A1/A3 normalize to the same base campaign, but identity is absent.
        proven, suspected = classify_duplicate_records([first, second])
        self.assertFalse(proven)
        self.assertEqual(len(suspected), 1)
        independent = dict(second, plan="DifferentPlan")
        _, suspected = classify_duplicate_records([first, independent])
        self.assertFalse(suspected)


class GateTests(unittest.TestCase):
    def test_reconciliation_failure_blocks(self):
        with tempfile.TemporaryDirectory() as root:
            paths = RuntimePaths.for_workspace(Path(root))
            memory = LocalMemory(paths)
            memory.add_mapping("campaign", "full_store_export", "Store-A", gate_id="G")
            template = minimal_template(["P"])
            template["store_groups"] = [{"store": "Store-A", "products": []}]
            template["sku"] = {"map": {"sku-1": {"product": "P", "spec": "single"}}, "products": []}
            campaign = {"kind": "campaign_full_store", "name": "full.csv", "total_cents": 900, "records": [{"row": 2, "plan": "sku-1", "record_id": "", "sku": "sku-1", "cost_cents": 900, "raw": {}}]}
            _, _, gates = build_golden_payload(template, financial_record("Store-A", 1000), [campaign], None, memory, {}, "2026-08-18")
            self.assertTrue(any(gate["gate_type"] == "HG-04" for gate in gates))

    def test_external_nonzero_sales_sku_blocks(self):
        with tempfile.TemporaryDirectory() as root:
            memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
            template = minimal_template(["P"])
            template["sku"] = {"map": {"known": {"product": "P", "spec": "single"}}, "products": [{"name": "P", "brush_value": 1}]}
            sales = {"records": [{"row": 2, "sku": "external", "amount_cents": 100}], "reported_cents": 100}
            _, _, gates = build_golden_payload(template, financial_record("Store-P", 0), [], sales, memory, {}, "2026-08-18")
            self.assertTrue(any(gate["gate_type"] == "HG-05" for gate in gates))


if __name__ == "__main__":
    unittest.main()
