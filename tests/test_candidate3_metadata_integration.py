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


class ProductionMetadataIntegrationTests(unittest.TestCase):
    def _run_scenario(self, root: Path, *, target: str, product_alias: str, plan_root: str) -> tuple[dict, MagicMock]:
        workspace = root / "workspace"
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        (input_dir / "template.xlsx").write_bytes(b"synthetic-template-placeholder")
        (input_dir / "finance.xlsx").write_bytes(b"synthetic-financial-placeholder")
        (input_dir / "regular.csv").write_text(
            "计划名称,商品名称,花费,日期\n"
            f"{target},{target},1.00,{TARGET_DATE}\n"
            f"{plan_root}1,,2.00,{TARGET_DATE}\n"
            f"{plan_root}3,,3.00,{TARGET_DATE}\n",
            encoding="utf-8-sig",
        )
        # This unrelated current-run file prevents the global solver from
        # pre-assigning the regular export.  The regular export then obtains
        # STORE_A naturally from product membership plus exact reconciliation.
        (input_dir / "blocker.csv").write_text(
            "计划名称,SKU,花费,日期\n"
            f"FILE_BLOCKER,UNKNOWN_SKU,0.50,{TARGET_DATE}\n",
            encoding="utf-8-sig",
        )

        template = {
            "report": {
                "sheet": "REPORT",
                "template_date": TARGET_DATE,
                "date_cell": "A1",
                "total_cost_cell": "C8",
                "total_sales_cell": "D8",
                "total_roi_cell": "E8",
                "products": [{
                    "name": target,
                    "row": 4,
                    "cost_cell": "C4",
                    "sales_cell": "D4",
                    "roi_cell": "E4",
                }],
            },
            "sku": None,
            "store_groups": [{"store": STORE, "products": [product_alias]}],
        }
        financial = {
            "sheets": [{
                "name": "FINANCE",
                "values": [
                    ["流水单号", "投放账户名称", "投放日期", "交易类型", "支出"],
                    ["TX-SYNTHETIC-C3", STORE, TARGET_DATE, "快车扣费", "6.00"],
                ],
            }],
        }
        bridge = MagicMock(name="candidate3_synthetic_workbook_bridge")
        bridge.inspect_template.side_effect = lambda path: (
            deepcopy(template) if Path(path).name == "template.xlsx" else None
        )
        bridge.inspect_xlsx.return_value = financial
        dependencies = {"dependency_check": "PASS", "node": "node", "node_modules": "node_modules"}

        with (
            patch("daily_roi_lib.dependency_preflight", return_value=dependencies),
            patch("daily_roi_lib.WorkbookBridge", return_value=bridge),
        ):
            state = roi.run_report(workspace, input_dir, output_dir)
        return state, bridge

    def _assert_one_persisted_business_decision(
        self,
        state: dict,
        bridge: MagicMock,
        *,
        expected_sources: set[str],
        expected_target: str,
        require_union_provenance: bool = False,
    ) -> None:
        batch = state["review_batch"]
        matching = [
            item for item in batch["items"]
            if expected_sources.issubset(set(item["sources"]))
        ]
        self.assertEqual(len(matching), 1)
        item = matching[0]
        self.assertEqual(item["decision_type"], evidence.INFERRED_REVIEW)
        self.assertEqual(item["proposed_answer"], expected_target)
        self.assertEqual(set(item["sources"]), expected_sources)
        member_sources = {
            source
            for member in item["resolution_candidate"]["members"]
            for source in member["sources"]
        }
        self.assertEqual(member_sources, expected_sources)
        if require_union_provenance:
            self.assertIn(
                "cross_path_semantic_evidence_union",
                {proof["type"] for proof in item["supporting_evidence"]},
            )

        paths = roi.RuntimePaths.for_workspace(Path(state["workspace"]))
        persisted_state = json.loads(paths.current_run.read_text(encoding="utf-8"))
        persisted_batch = json.loads(
            (paths.runs_dir / state["run_id"] / "review-batch.json").read_text(encoding="utf-8")
        )
        for persisted in (persisted_state["review_batch"], persisted_batch):
            decisions = [
                candidate for candidate in persisted["items"]
                if expected_sources.issubset(set(candidate["sources"]))
            ]
            self.assertEqual(len(decisions), 1)
            self.assertEqual(set(decisions[0]["sources"]), expected_sources)
        self.assertEqual(
            persisted_state["audit"]["resolution_summary"]["inferred_review_count"],
            len(persisted_state["review_batch"]["items"]),
        )
        for gate in state["gates"]:
            resolution = ((gate.get("evidence") or {}).get("resolution") or {})
            self.assertTrue(expected_sources.isdisjoint(set(resolution.get("sources") or [])))
        self.assertEqual(state["output_path"], None)
        bridge.write.assert_not_called()
        self.assertFalse(paths.memory.exists())

    def test_production_cross_entity_uses_reconciliation_store_scope(self):
        with tempfile.TemporaryDirectory() as temp_root:
            state, bridge = self._run_scenario(
                Path(temp_root),
                target="ROOT_A液",
                product_alias="ROOT_A",
                plan_root="ROOT_A",
            )
            expected = {"ROOT_A", "ROOT_A1", "ROOT_A3"}
            self._assert_one_persisted_business_decision(
                state,
                bridge,
                expected_sources=expected,
                expected_target="ROOT_A液",
            )

    def test_production_semantic_union_uses_resolver_source_root(self):
        with tempfile.TemporaryDirectory() as temp_root:
            state, bridge = self._run_scenario(
                Path(temp_root),
                target="ROOT_SX液",
                product_alias="ROOT_S棒",
                plan_root="ROOT_S",
            )
            expected = {"ROOT_S棒", "ROOT_S1", "ROOT_S3"}
            self._assert_one_persisted_business_decision(
                state,
                bridge,
                expected_sources=expected,
                expected_target="ROOT_SX液",
                require_union_provenance=True,
            )

    @staticmethod
    def _decision(
        source: str,
        *,
        entity_type: str,
        root: str,
        contexts: list[dict] | None = None,
        reconciliation: dict | None = None,
        evidence_items: list[dict] | None = None,
        alternatives: list[str] | None = None,
        contradictions: list[dict] | None = None,
        candidate: str | None = "TARGET_A",
        decision: str = evidence.INFERRED_REVIEW,
        sources: list[str] | None = None,
    ) -> dict:
        return evidence.finalize_resolution({
            "schema_version": 1,
            "entity_type": entity_type,
            "source": source,
            "sources": sources or [source],
            "fact_family": f"{entity_type}:{root}",
            "semantic_root": root,
            "business_relation_kind": "PRODUCT_ASSIGNMENT",
            "candidate": candidate,
            "decision": decision,
            "evidence": evidence_items or [{"type": "unique_candidate_in_current_template"}],
            "contradictions": contradictions or [],
            "alternatives": alternatives or [],
            "candidate_generation": [],
            "reconciliation": reconciliation or {"status": "NOT_TESTED"},
            "contexts": contexts or [],
            "target_kind": "product",
        })

    def test_failed_reconciliation_store_is_not_effective_scope(self):
        decision = self._decision(
            "PLAN_A_1",
            entity_type="campaign",
            root="ROOT_A",
            reconciliation={"status": "FAIL", "store": "STORE_A"},
        )
        projection = evidence._decision_store_projection(decision)
        self.assertEqual(projection["stores"], set())
        self.assertEqual(projection["sources"]["RECONCILIATION_PASS"], set())

    def test_context_reconciliation_store_contradiction_blocks_merge(self):
        product = self._decision(
            "PRODUCT_ALIAS_A",
            entity_type="product",
            root="ROOT_A",
            contexts=[{"template_store": "STORE_A"}],
            reconciliation={"status": "PASS", "store": "STORE_B"},
        )
        campaign = self._decision(
            "PLAN_A_1",
            entity_type="campaign",
            root="ROOT_A",
            contexts=[{"store": "STORE_A"}],
            reconciliation={"status": "PASS", "store": "STORE_B"},
        )
        self.assertTrue(evidence._decision_store_projection(product)["conflicted"])
        self.assertEqual(len(evidence.merge_inferred_decisions([product, campaign])), 2)

    def test_non_semantic_nested_source_root_is_ignored(self):
        product = self._decision(
            "ROOT_LONG",
            entity_type="product",
            root="",
            contexts=[{"template_store": "STORE_A"}],
            evidence_items=[{"type": "current_run_evidence_only", "source_root": "ROOT_SHORT"}],
        )
        campaign = self._decision(
            "ROOT_SHORT1",
            entity_type="campaign",
            root="ROOT_SHORT",
            contexts=[{"store": "STORE_A"}],
        )
        projection = evidence._decision_root_projection(product)
        self.assertNotIn("rootshort", projection["roots"])
        self.assertEqual(len(evidence.merge_inferred_decisions([product, campaign])), 2)

    def test_conflicting_semantic_root_provenance_blocks_merge(self):
        product = self._decision(
            "PRODUCT_ALIAS_A",
            entity_type="product",
            root="ROOT_A",
            contexts=[{"template_store": "STORE_A"}],
            evidence_items=[
                {"type": "normalized_containment", "source_root": "ROOT_B", "candidate_root": "TARGET_A"},
            ],
        )
        campaign = self._decision(
            "PLAN_A_1",
            entity_type="campaign",
            root="ROOT_A",
            contexts=[{"store": "STORE_A"}],
        )
        self.assertTrue(evidence._decision_root_projection(product)["conflicted"])
        self.assertEqual(len(evidence.merge_inferred_decisions([product, campaign])), 2)

    def test_semantic_root_projection_does_not_bypass_competing_target_guard(self):
        product = self._decision(
            "ROOT_LONG",
            entity_type="product",
            root="",
            contexts=[{"template_store": "STORE_A"}],
            evidence_items=[
                {"type": "normalized_containment", "source_root": "ROOT_SHORT", "candidate_root": "ROOT_SHORTX"},
                {"type": "evidence_supported_preference", "preferred": "TARGET_A"},
            ],
            alternatives=["TARGET_B"],
            decision=evidence.HUMAN_REQUIRED,
        )
        campaign = self._decision(
            "ROOT_SHORT1",
            entity_type="campaign",
            root="ROOT_SHORT",
            reconciliation={"status": "PASS", "store": "STORE_A"},
            evidence_items=[
                {"type": "sibling_alias_family", "sources": ["ROOT_SHORT1", "ROOT_SHORT3"]},
                {"type": "reconciliation_consistent", "store": "STORE_A"},
            ],
            candidate=None,
            decision=evidence.HUMAN_REQUIRED,
            sources=["ROOT_SHORT1", "ROOT_SHORT3"],
        )
        united = evidence.union_cross_path_evidence([product, campaign])
        self.assertEqual({item["decision"] for item in united}, {evidence.HUMAN_REQUIRED})

    def test_pair_e_style_surviving_alternative_remains_unmerged(self):
        product = self._decision(
            "PRODUCT_ALIAS_E",
            entity_type="product",
            root="ROOT_E",
            contexts=[{"template_store": "STORE_A"}],
            alternatives=["TARGET_B"],
        )
        campaign = self._decision(
            "PLAN_E_1",
            entity_type="campaign",
            root="ROOT_E",
            reconciliation={"status": "PASS", "store": "STORE_A"},
            alternatives=["TARGET_B"],
        )
        self.assertEqual(len(evidence.merge_inferred_decisions([product, campaign])), 2)


if __name__ == "__main__":
    unittest.main()
