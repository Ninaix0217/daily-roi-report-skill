from __future__ import annotations

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
    atomic_json,
    build_golden_payload,
    make_gate,
    norm,
    parse_campaign,
    resolve_gate,
)
from evidence_resolution import HUMAN_REQUIRED, MACHINE_INFERRED, VERIFIED, resolve_entity  # noqa: E402


def template(products: list[str], *, store: str | None = None, skus: dict | None = None) -> dict:
    model = {
        "report": {
            "products": [
                {"name": name, "row": row, "cost_cell": f"C{row}", "sales_cell": f"D{row}", "roi_cell": f"E{row}"}
                for row, name in enumerate(products, 4)
            ],
            "template_date": "2026-08-20",
        },
        "sku": {"map": skus or {}, "products": [], "conflicts": []} if skus is not None else None,
        "store_groups": [{"store": store, "products": products}] if store else [],
    }
    return model


def financial(store: str, cents: int) -> dict:
    return {
        "source_name": "financial.xls",
        "records": [{
            "row": 2,
            "transaction_id": "tx-1",
            "store": store,
            "date": "2026-08-20",
            "transaction_type": "快车扣费",
            "expense_cents": cents,
        }],
    }


def regular(records: list[tuple[str, int]], total_name: str = "regular.csv") -> dict:
    rows = [
        {"row": row, "plan": plan, "record_id": f"r-{row}", "sku": "", "cost_cents": cents, "raw": {}}
        for row, (plan, cents) in enumerate(records, 2)
    ]
    return {"kind": "campaign_regular", "name": total_name, "total_cents": sum(item["cost_cents"] for item in rows), "records": rows}


class EvidenceDecisionTests(unittest.TestCase):
    def test_exact_identity_is_verified(self):
        decision = resolve_entity(
            "item-100",
            ["Product A"],
            entity_type="product",
            exact_matches=[("Product A", "exact_template_sku", {"sku": "item-100"})],
        )
        self.assertEqual(decision["decision"], VERIFIED)
        self.assertEqual(decision["candidate"], "Product A")
        self.assertIn("exact_template_sku", {item["type"] for item in decision["evidence"]})
        self.assertNotIn("confidence", decision)

    def test_platform_item_id_header_is_parsed_as_identity(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "campaign.csv"
            path.write_text("计划名称,投放商品 ID,花费\n智能推广,100200300,10.00\n", encoding="utf-8-sig")
            parsed = parse_campaign(path)
            self.assertEqual(parsed["kind"], "campaign_full_store")
            self.assertEqual(parsed["identity_header"], "投放商品 ID")
            self.assertEqual(parsed["records"][0]["sku"], "100200300")
            self.assertEqual(parsed["records"][0]["record_id"], "")

    def test_weak_semantic_stem_needs_context_and_corroboration(self):
        unresolved = resolve_entity("舒1", ["舒润液", "其他产品"], entity_type="campaign")
        self.assertEqual(unresolved["decision"], HUMAN_REQUIRED)
        resolved = resolve_entity(
            "舒1",
            ["舒润液", "其他产品"],
            entity_type="campaign",
            context_candidates=["舒润液", "其他产品"],
            sibling_sources=["舒1", "舒3"],
            reconciliation={"status": "PASS"},
        )
        self.assertEqual(resolved["decision"], MACHINE_INFERRED)

    def test_competing_semantic_candidates_remain_human_required(self):
        decision = resolve_entity(
            "舒1",
            ["舒润液", "舒缓液"],
            entity_type="campaign",
            context_candidates=["舒润液", "舒缓液"],
            sibling_sources=["舒1", "舒3"],
            reconciliation={"status": "PASS"},
        )
        self.assertEqual(decision["decision"], HUMAN_REQUIRED)
        self.assertEqual(set(decision["alternatives"]), {"舒润液", "舒缓液"})

    def test_unique_stable_token_rejects_common_prefix_only_candidate(self):
        decision = resolve_entity(
            "xy Example-alpha",
            ["Example Pharma-alpha", "Example Pharma-beta"],
            entity_type="store",
        )
        self.assertEqual(decision["decision"], MACHINE_INFERRED)
        self.assertEqual(decision["candidate"], "Example Pharma-alpha")
        evidence_types = {item["type"] for item in decision["evidence"]}
        self.assertIn("unique_shared_token", evidence_types)
        self.assertIn("weaker_semantic_candidates_rejected", evidence_types)


class EvidenceWorkflowTests(unittest.TestCase):
    def test_full_store_exact_sku_and_unique_store_auto_resolve(self):
        with tempfile.TemporaryDirectory() as root:
            model = template(
                ["Product A"],
                store="Store-Product A",
                skus={"100200300": {"product": "Product A", "spec": "single"}},
            )
            campaign = {
                "kind": "campaign_full_store",
                "name": "full.csv",
                "total_cents": 1000,
                "records": [{"row": 2, "plan": "智能推广", "record_id": "", "sku": "100200300", "cost_cents": 1000, "raw": {}}],
            }
            payload, audit, gates = build_golden_payload(
                model,
                financial("Store-Product A", 1000),
                [campaign],
                None,
                LocalMemory(RuntimePaths.for_workspace(Path(root))),
                {},
                "2026-08-20",
            )
            self.assertFalse(gates)
            self.assertEqual(payload["expense_total_cents"], 1000)
            sku_decisions = [item for item in audit["resolutions"] if item["source"] == "100200300"]
            self.assertEqual(sku_decisions[0]["decision"], VERIFIED)

    def test_generic_plan_uses_uniquely_known_store_main_product(self):
        with tempfile.TemporaryDirectory() as root:
            model = template(["Main Product"], store="Store-Main Product")
            campaign = regular([("Main Product", 400), ("单盒", 600)])
            payload, audit, gates = build_golden_payload(
                model,
                financial("Store-Main Product", 1000),
                [campaign],
                None,
                LocalMemory(RuntimePaths.for_workspace(Path(root))),
                {},
                "2026-08-20",
            )
            self.assertFalse(gates)
            self.assertEqual(payload["expense_total_cents"], 1000)
            generic = [item for item in audit["resolutions"] if item["source"] == "单盒"]
            self.assertEqual(generic[0]["decision"], VERIFIED)

    def test_alias_family_auto_resolves_after_store_and_reconciliation(self):
        with tempfile.TemporaryDirectory() as root:
            model = template(["舒润液", "Anchor Product"], store="Store-舒润液")
            campaign = regular([("Anchor Product", 100), ("舒1", 400), ("舒3", 500)])
            payload, audit, gates = build_golden_payload(
                model,
                financial("Store-舒润液", 1000),
                [campaign],
                None,
                LocalMemory(RuntimePaths.for_workspace(Path(root))),
                {},
                "2026-08-20",
            )
            self.assertFalse(gates)
            self.assertEqual(payload["expense_total_cents"], 1000)
            inferred = [item for item in audit["resolutions"] if item["source"] in {"舒1", "舒3"}]
            self.assertEqual({item["decision"] for item in inferred}, {MACHINE_INFERRED})

    def test_ambiguous_alias_family_emits_one_human_question(self):
        with tempfile.TemporaryDirectory() as root:
            model = template(["舒润液", "舒缓液", "Anchor Product"], store="Store-舒润液")
            campaign = regular([("Anchor Product", 100), ("舒1", 400), ("舒3", 500)])
            _, _, gates = build_golden_payload(
                model,
                financial("Store-舒润液", 1000),
                [campaign],
                None,
                LocalMemory(RuntimePaths.for_workspace(Path(root))),
                {},
                "2026-08-20",
            )
            mapping_gates = [gate for gate in gates if gate["gate_type"] == "HG-06" and gate.get("candidate_resolution")]
            self.assertEqual(len(gates), 1)
            self.assertEqual(len(mapping_gates), 1)
            self.assertEqual(set(mapping_gates[0]["candidate_resolution"]["sources"]), {"舒1", "舒3"})

    def test_semantic_store_alias_auto_resolves_when_unique(self):
        with tempfile.TemporaryDirectory() as root:
            model = template(
                ["Product A"],
                store="Example Pharma-alpha",
                skus={"100200300": {"product": "Product A", "spec": "single"}},
            )
            campaign = {
                "kind": "campaign_full_store",
                "name": "full.csv",
                "total_cents": 1000,
                "records": [{"row": 2, "plan": "智能推广", "record_id": "", "sku": "100200300", "cost_cents": 1000, "raw": {}}],
            }
            payload, audit, gates = build_golden_payload(
                model,
                financial("xy Example-alpha", 1000),
                [campaign],
                None,
                LocalMemory(RuntimePaths.for_workspace(Path(root))),
                {},
                "2026-08-20",
            )
            self.assertFalse(gates)
            self.assertEqual(payload["expense_total_cents"], 1000)
            store_decisions = [item for item in audit["resolutions"] if item["entity_type"] == "store" and item["source"] == "xy Example-alpha"]
            self.assertEqual(store_decisions[0]["decision"], MACHINE_INFERRED)

    def test_conflicting_template_sku_is_not_auto_resolved(self):
        with tempfile.TemporaryDirectory() as root:
            model = template(
                ["Product A", "Product B"],
                store="Store-Product A",
                skus={"100200300": {"product": "Product A", "spec": "single"}},
            )
            model["sku"]["conflicts"] = [{"sku": "100200300", "products": ["Product A", "Product B"]}]
            campaign = {
                "kind": "campaign_full_store",
                "name": "full.csv",
                "total_cents": 1000,
                "records": [{"row": 2, "plan": "智能推广", "record_id": "", "sku": "100200300", "cost_cents": 1000, "raw": {}}],
            }
            _, _, gates = build_golden_payload(
                model,
                financial("Store-Product A", 1000),
                [campaign],
                None,
                LocalMemory(RuntimePaths.for_workspace(Path(root))),
                {},
                "2026-08-20",
            )
            self.assertEqual([gate["gate_type"] for gate in gates], ["HG-05"])
            contradiction_types = {item["type"] for item in gates[0]["evidence"]["resolution"]["contradictions"]}
            self.assertIn("conflicting_exact_targets", contradiction_types)

    def test_grouped_human_confirmation_persists_each_source(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = RuntimePaths.for_workspace(workspace)
            gate = make_gate(
                "HG-06",
                "ambiguous family",
                {"sources": ["舒1", "舒3"]},
                "confirm",
                candidate={"entity_type": "campaign", "source": "舒1", "sources": ["舒1", "舒3"], "target": "舒润液"},
                persistence={"entity_type": "campaign", "source": "舒1", "sources": ["舒1", "舒3"], "target": "舒润液"},
            )
            atomic_json(paths.current_run, {
                "schema_version": 1,
                "run_id": "grouped",
                "gates": [gate],
                "run_mappings": {},
                "input_dir": str(workspace / "input"),
                "output_dir": str(workspace / "output"),
            })
            with patch("daily_roi_lib.run_report", return_value={"status": "COMPLETE"}):
                resolve_gate(workspace, gate["gate_id"], target=None, persistence="PERSISTENT_REUSABLE")
            memory = LocalMemory(paths)
            self.assertEqual(memory.resolve("campaign", "舒1"), "舒润液")
            self.assertEqual(memory.resolve("campaign", "舒3"), "舒润液")

    def test_regular_file_store_confirmation_has_resumable_mapping_key(self):
        with tempfile.TemporaryDirectory() as root:
            model = template(["Shared Product"])
            model["store_groups"] = [
                {"store": "Store A", "products": ["Shared Product"]},
                {"store": "Store B", "products": ["Shared Product"]},
            ]
            ledger = {
                "source_name": "financial.xls",
                "records": [
                    {"row": 2, "transaction_id": "tx-a", "store": "Store A", "date": "2026-08-20", "transaction_type": "快车扣费", "expense_cents": 1000},
                    {"row": 3, "transaction_id": "tx-b", "store": "Store B", "date": "2026-08-20", "transaction_type": "快车扣费", "expense_cents": 1000},
                ],
            }
            campaign = regular([("Shared Product", 1000)])
            memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
            _, _, first_gates = build_golden_payload(model, ledger, [campaign], None, memory, {}, "2026-08-20")
            store_gate = next(gate for gate in first_gates if (gate.get("candidate_resolution") or {}).get("entity_type") == "campaign")
            token = store_gate["candidate_resolution"]["source"]
            run_mappings = {f"campaign:{norm(token)}": "Store A"}
            _, _, second_gates = build_golden_payload(model, ledger, [campaign], None, memory, run_mappings, "2026-08-20")
            repeated = [gate for gate in second_gates if (gate.get("candidate_resolution") or {}).get("source") == token]
            self.assertFalse(repeated)


if __name__ == "__main__":
    unittest.main()
