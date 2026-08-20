from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_roi_lib import (  # noqa: E402
    LocalMemory,
    RuntimePaths,
    WORKFLOW_RULE_STALE_TEMPLATE,
    atomic_json,
    build_golden_payload,
    classify_duplicate_records,
    evaluate_dates,
    make_gate,
    resolve_gate,
)


def template(products: list[str]) -> dict:
    return {
        "report": {
            "products": [
                {"name": name, "row": row, "cost_cell": f"C{row}", "sales_cell": f"D{row}", "roi_cell": f"E{row}"}
                for row, name in enumerate(products, 4)
            ],
            "template_date": "2026-08-18",
        },
        "sku": None,
        "store_groups": [],
    }


def financial(store: str, cents: int) -> dict:
    return {
        "source_name": "financial.xls",
        "records": [{
            "row": 2,
            "transaction_id": "tx-1",
            "store": store,
            "date": "2026-08-18",
            "transaction_type": "快车扣费",
            "expense_cents": cents,
        }],
    }


class HumanGateEvalSuite(unittest.TestCase):
    def test_eval_a_unknown_mapping_requires_human(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, gates = build_golden_payload(
                template(["标准产品"]),
                financial("示例店铺-来源产品", 1500),
                [],
                None,
                LocalMemory(RuntimePaths.for_workspace(Path(root))),
                {},
                "2026-08-18",
            )
            self.assertEqual([gate["gate_type"] for gate in gates], ["HG-01"])
            self.assertEqual(gates[0]["status"], "HUMAN_REQUIRED")
            self.assertEqual(gates[0]["candidate_resolution"]["source"], "来源产品")
            self.assertEqual(gates[0]["candidate_resolution"]["target"], "标准产品")
            self.assertEqual(gates[0]["evidence"]["resolution"]["decision"], "HUMAN_REQUIRED")

    def test_eval_b_confirm_persist_and_resume(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = RuntimePaths.for_workspace(workspace)
            gate = make_gate(
                "HG-01",
                "unknown nonzero product mapping",
                {"source": "来源产品", "amount": "15.00"},
                "confirm mapping",
                candidate={"entity_type": "product", "source": "来源产品", "target": None},
                persistence={"entity_type": "product", "source": "来源产品", "target": None},
            )
            atomic_json(paths.current_run, {
                "schema_version": 1,
                "run_id": "eval-b",
                "gates": [gate],
                "run_mappings": {},
                "input_dir": str(workspace / "input"),
                "output_dir": str(workspace / "output"),
            })
            with patch("daily_roi_lib.run_report", return_value={"status": "COMPLETE", "run_id": "eval-b"}) as resumed:
                result = resolve_gate(workspace, gate["gate_id"], target="标准产品", persistence="PERSISTENT_REUSABLE")
            self.assertEqual(result["status"], "COMPLETE")
            self.assertTrue(resumed.called)
            self.assertEqual(LocalMemory(paths).resolve("product", "来源产品"), "标准产品")
            audit = paths.confirmations.read_text(encoding="utf-8")
            self.assertIn("PERSISTENT_REUSABLE", audit)

    def test_eval_c_learned_mapping_reused_without_gate(self):
        with tempfile.TemporaryDirectory() as root:
            paths = RuntimePaths.for_workspace(Path(root))
            memory = LocalMemory(paths)
            memory.add_mapping("product", "来源产品", "标准产品", gate_id="eval-c")
            payload, _, gates = build_golden_payload(
                template(["标准产品"]), financial("示例店铺-来源产品", 1500), [], None,
                LocalMemory(paths), {}, "2026-08-18",
            )
            self.assertFalse(gates)
            self.assertEqual(payload["expense_total_cents"], 1500)

    def test_eval_d_memory_isolation(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            paths_a = RuntimePaths.for_workspace(Path(a))
            paths_b = RuntimePaths.for_workspace(Path(b))
            LocalMemory(paths_a).add_mapping("product", "来源产品", "标准产品", gate_id="eval-d")
            _, _, gates_a = build_golden_payload(template(["标准产品"]), financial("示例店铺-来源产品", 1500), [], None, LocalMemory(paths_a), {}, "2026-08-18")
            _, _, gates_b = build_golden_payload(template(["标准产品"]), financial("示例店铺-来源产品", 1500), [], None, LocalMemory(paths_b), {}, "2026-08-18")
            self.assertFalse(gates_a)
            self.assertEqual([gate["gate_type"] for gate in gates_b], ["HG-01"])

    def test_eval_e_stale_template_date_auto_updates_with_rule(self):
        with tempfile.TemporaryDirectory() as root:
            memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
            memory.add_rule(WORKFLOW_RULE_STALE_TEMPLATE, gate_id="eval-e")
            target, update, gates = evaluate_dates(
                {"financial": ["2026-08-17"], "sales": ["2026-08-17"], "campaign": ["2026-08-17"]},
                "2026-08-16",
                memory,
            )
            self.assertEqual((target, update, gates), ("2026-08-17", True, []))

    def test_eval_f_source_date_conflict_requires_human(self):
        with tempfile.TemporaryDirectory() as root:
            memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
            memory.add_rule(WORKFLOW_RULE_STALE_TEMPLATE, gate_id="eval-f")
            _, update, gates = evaluate_dates(
                {"financial": ["2026-08-18"], "sales": ["2026-08-18"], "campaign": ["2026-08-17"]},
                "2026-08-16",
                memory,
            )
            self.assertFalse(update)
            self.assertEqual([gate["gate_type"] for gate in gates], ["HG-02"])

    def test_eval_g_suspected_duplicate_is_not_silently_removed(self):
        records = [
            {"source_kind": "regular", "record_id": "", "raw": {"plan": "Plan1"}, "resolved_product": "P", "cost_cents": 1000, "plan": "Plan1", "date": "2026-08-18"},
            {"source_kind": "full", "record_id": "", "raw": {"plan": "Plan3"}, "resolved_product": "P", "cost_cents": 1000, "plan": "Plan3", "date": "2026-08-18"},
        ]
        proven, suspected = classify_duplicate_records(records)
        self.assertFalse(proven)
        self.assertEqual(len(suspected), 1)

    def test_eval_h_external_nonzero_sku_requires_human(self):
        with tempfile.TemporaryDirectory() as root:
            model = template(["P"])
            model["sku"] = {"map": {"known": {"product": "P", "spec": "single"}}, "products": [{"name": "P", "brush_value": 1}]}
            sales = {"records": [{"row": 2, "sku": "external", "amount_cents": 100}], "reported_cents": 100}
            _, _, gates = build_golden_payload(model, financial("Store-P", 0), [], sales, LocalMemory(RuntimePaths.for_workspace(Path(root))), {}, "2026-08-18")
            self.assertIn("HG-05", [gate["gate_type"] for gate in gates])


if __name__ == "__main__":
    unittest.main()
