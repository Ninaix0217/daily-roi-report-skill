from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daily_roi  # noqa: E402
import daily_roi_lib as roi  # noqa: E402
import evidence_resolution as evidence  # noqa: E402
import review_ux  # noqa: E402


def synthetic_decision(
    source: str,
    *,
    entity_type: str,
    candidate: str | None,
    decision: str,
    store: str = "STORE_A",
    root: str = "ROOT_A",
    sources: list[str] | None = None,
    relation: str = "PRODUCT_ASSIGNMENT",
    evidence_items: list[dict] | None = None,
    contradictions: list[dict] | None = None,
    alternatives: list[str] | None = None,
    target_kind: str = "product",
) -> dict:
    return evidence.finalize_resolution({
        "schema_version": 1,
        "entity_type": entity_type,
        "source": source,
        "sources": sources or [source],
        "fact_family": f"{entity_type}:{root}",
        "semantic_root": root,
        "business_relation_kind": relation,
        "candidate": candidate,
        "decision": decision,
        "evidence": evidence_items or [],
        "contradictions": contradictions or [],
        "alternatives": alternatives or [],
        "candidate_generation": [],
        "reconciliation": {"status": "PASS", "difference_cents": 0},
        "contexts": [{"store": store}],
        "target_kind": target_kind,
    })


def product_side(*, store: str = "STORE_A", contradictions: list[dict] | None = None, alternatives: list[str] | None = None) -> dict:
    return synthetic_decision(
        "PRODUCT_ALIAS_A",
        entity_type="product",
        candidate="PRODUCT_X",
        decision=evidence.HUMAN_REQUIRED,
        store=store,
        evidence_items=[
            {"type": "weak_shared_anchor", "anchor": "ROOT_A"},
            {"type": "current_store_product_scope", "candidate_count": 1},
            {"type": "unique_candidate_in_current_template"},
            {"type": "evidence_supported_preference", "preferred": "PRODUCT_X"},
        ],
        contradictions=contradictions,
        alternatives=alternatives,
    )


def campaign_side(*, store: str = "STORE_A", contradictions: list[dict] | None = None, alternatives: list[str] | None = None) -> dict:
    return synthetic_decision(
        "PLAN_A_1",
        entity_type="campaign",
        candidate=None,
        decision=evidence.HUMAN_REQUIRED,
        store=store,
        sources=["PLAN_A_1", "PLAN_A_3"],
        evidence_items=[
            {"type": "sibling_alias_family", "sources": ["PLAN_A_1", "PLAN_A_3"]},
            {"type": "reconciliation_consistent", "difference_cents": 0},
        ],
        contradictions=contradictions,
        alternatives=alternatives,
    )


def inferred_member(
    source: str,
    *,
    entity_type: str,
    store: str = "STORE_A",
    relation: str = "PRODUCT_ASSIGNMENT",
    sources: list[str] | None = None,
    run_only: bool = False,
) -> dict:
    item = synthetic_decision(
        source,
        entity_type=entity_type,
        candidate="PRODUCT_X",
        decision=evidence.INFERRED_REVIEW,
        store=store,
        relation=relation,
        sources=sources,
        evidence_items=[
            {"type": "cross_path_semantic_evidence_union", "root": "ROOT_A"},
            {"type": "unique_candidate_in_current_template"},
            {"type": "current_store_product_scope", "candidate_count": 1},
        ],
    )
    if run_only:
        item["run_only"] = True
    return item


def review_state(workspace: Path, batch: dict) -> dict:
    return {
        "schema_version": 1,
        "run_id": "candidate-2-synthetic-run",
        "status": evidence.INFERRED_REVIEW,
        "stage": "RESOLVE",
        "created_at": "2026-08-22T00:00:00+08:00",
        "input_dir": str(workspace / "input"),
        "output_dir": str(workspace / "output"),
        "manifest": [],
        "template_model": {
            "report": {"products": [{"name": "PRODUCT_X"}]},
            "store_groups": [{"store": "STORE_A", "products": ["PRODUCT_X"]}],
        },
        "gates": [],
        "review_batch": batch,
        "review_metrics": {},
        "run_mappings": {},
    }


class CrossPathSemanticEvidenceUnionTests(unittest.TestCase):
    def test_candidate_missing_path_retains_sibling_and_reconciliation_evidence_for_union(self):
        campaign = evidence.resolve_entity(
            "PLAN_A_1",
            ["PRODUCT_X"],
            entity_type="campaign",
            semantic_candidates=[],
            sibling_sources=["PLAN_A_1", "PLAN_A_3"],
            reconciliation={"status": "PASS", "difference_cents": 0},
        )
        campaign.update(
            semantic_root="ROOT_A",
            business_relation_kind="PRODUCT_ASSIGNMENT",
            contexts=[{"store": "STORE_A"}],
            sources=["PLAN_A_1", "PLAN_A_3"],
        )
        self.assertEqual((campaign["decision"], campaign["candidate"]), (evidence.HUMAN_REQUIRED, None))
        self.assertIn("sibling_alias_family", {item["type"] for item in campaign["evidence"]})
        united = evidence.union_cross_path_evidence([product_side(), campaign])
        self.assertEqual({item["decision"] for item in united}, {evidence.INFERRED_REVIEW})

    def test_semantic_evidence_union_positive(self):
        united = evidence.union_cross_path_evidence([product_side(), campaign_side()])
        self.assertEqual({item["decision"] for item in united}, {evidence.INFERRED_REVIEW})
        self.assertEqual({item["candidate"] for item in united}, {"PRODUCT_X"})
        self.assertTrue(all("cross_path_semantic_evidence_union" in {proof["type"] for proof in item["evidence"]} for item in united))

    def test_semantic_union_cross_store_negative(self):
        united = evidence.union_cross_path_evidence([product_side(store="STORE_A"), campaign_side(store="STORE_B")])
        self.assertEqual({item["decision"] for item in united}, {evidence.HUMAN_REQUIRED})

    def test_semantic_union_hard_identity_contradiction(self):
        conflict = {"type": "hard_identity_conflict", "current_target": "PRODUCT_Y"}
        united = evidence.union_cross_path_evidence([product_side(contradictions=[conflict]), campaign_side()])
        self.assertEqual({item["decision"] for item in united}, {evidence.HUMAN_REQUIRED})

    def test_semantic_union_surviving_competitor_remains_human_required(self):
        united = evidence.union_cross_path_evidence([
            product_side(alternatives=["PRODUCT_Y"]),
            campaign_side(alternatives=["PRODUCT_Y"]),
        ])
        self.assertEqual({item["decision"] for item in united}, {evidence.HUMAN_REQUIRED})


class CrossEntityConsolidationTests(unittest.TestCase):
    def test_cross_entity_consolidation_preserves_all_source_lineage(self):
        product = inferred_member("PRODUCT_ALIAS_A", entity_type="product")
        campaign = inferred_member("PLAN_A_1", entity_type="campaign", sources=["PLAN_A_1", "PLAN_A_3"])
        merged = evidence.merge_inferred_decisions([product, campaign])
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0]["sources"]), {"PRODUCT_ALIAS_A", "PLAN_A_1", "PLAN_A_3"})
        self.assertEqual(len(merged[0]["member_decisions"]), 2)
        self.assertEqual({item["entity_type"] for item in merged[0]["member_decisions"]}, {"product", "campaign"})

    def test_cross_store_false_merge_is_blocked(self):
        merged = evidence.merge_inferred_decisions([
            inferred_member("PRODUCT_ALIAS_A", entity_type="product", store="STORE_A"),
            inferred_member("PLAN_A_1", entity_type="campaign", store="STORE_B"),
        ])
        self.assertEqual(len(merged), 2)

    def test_incompatible_business_relation_false_merge_is_blocked(self):
        merged = evidence.merge_inferred_decisions([
            inferred_member("PRODUCT_ALIAS_A", entity_type="product", relation="PRODUCT_ASSIGNMENT"),
            inferred_member("FILE_A", entity_type="campaign", relation="STORE_ALLOCATION"),
        ])
        self.assertEqual(len(merged), 2)


class VerifiedCountTests(unittest.TestCase):
    def test_final_verified_count_has_one_canonical_source(self):
        resolutions = [
            synthetic_decision(
                f"BASE_VERIFIED_{index}",
                entity_type="product",
                candidate=f"PRODUCT_{index}",
                decision=evidence.VERIFIED,
                evidence_items=[{"type": "exact_template_product"}],
            )
            for index in range(82)
        ]
        resolutions.append(synthetic_decision(
            "WORKFLOW_VERIFIED",
            entity_type="workflow",
            candidate="CURRENT_DATE",
            decision=evidence.VERIFIED,
            relation="WORKFLOW_RULE",
            evidence_items=[{"type": "shared_core_deterministic_rule"}],
        ))
        batch = roi.make_review_batch([inferred_member("PLAN_A_1", entity_type="campaign")])
        batch["review_ux"] = review_ux.render_review_batch(batch, verified_records=82)
        audit = {
            "resolutions": resolutions,
            "review_batch": batch,
            "resolution_summary": {"verified": 82, "verified_count": 82, "inferred_review": 1, "human_required": 0},
        }
        roi.finalize_resolution_collection(audit, human_gates=[])
        machine = daily_roi.summary({"audit": audit, "review_batch": audit["review_batch"], "gates": []})
        self.assertEqual(audit["resolution_summary"]["verified_count"], 83)
        self.assertEqual(machine["VERIFIED_COUNT"], 83)
        self.assertIn("自动确认：83 项", audit["review_batch"]["review_ux"]["text"])


class GlobalAllocationHintTests(unittest.TestCase):
    def test_amount_only_unique_match_remains_human_required_with_hint(self):
        result = evidence.resolve_global_store_constraints([
            {"id": "FILE_A", "source": "FILE_A", "total_cents": 12345, "candidate_stores": [], "products": []},
            {"id": "FILE_B", "source": "FILE_B", "total_cents": 23456, "candidate_stores": [], "products": []},
        ], {"STORE_F": 35801})
        self.assertEqual(result["status"], evidence.HUMAN_REQUIRED)
        self.assertEqual(result["assignments"], {})
        self.assertEqual(result["decisions"], {})
        self.assertEqual(result["amount_only_hint"]["evidence_status"], "AMOUNT_ONLY_HINT")
        self.assertEqual(result["amount_only_hint"]["candidate"], "STORE_F")
        self.assertIsNone(result["amount_only_hint"]["selected_answer"])

    def test_non_amount_bridge_and_unique_solution_stays_inferred_review(self):
        result = evidence.resolve_global_store_constraints([
            {"id": "FILE_A", "source": "FILE_A", "total_cents": 12345, "candidate_stores": ["STORE_F"], "products": ["PRODUCT_X"]},
            {"id": "FILE_B", "source": "FILE_B", "total_cents": 23456, "candidate_stores": ["STORE_F"], "products": ["PRODUCT_X"]},
        ], {"STORE_F": 35801})
        self.assertEqual(result["status"], evidence.INFERRED_REVIEW)
        self.assertEqual({item["decision"] for item in result["decisions"].values()}, {evidence.INFERRED_REVIEW})

    def test_human_required_amount_hint_renders_outside_accept_batch(self):
        decision = campaign_side()
        decision["source"] = "FILE_A / FILE_B"
        decision["sources"] = ["FILE_A", "FILE_B"]
        decision["useful_hint"] = {
            "evidence_status": "AMOUNT_ONLY_HINT",
            "candidate": "STORE_F",
            "selected_answer": None,
        }
        gate = roi.resolution_gate(decision, "HG-06")
        batch = roi.make_review_batch([inferred_member("PLAN_A_1", entity_type="campaign")])
        rendered = review_ux.render_review_batch(batch, verified_records=1, human_gates=[gate])
        self.assertIn("### 需要你决定", rendered["text"])
        self.assertIn("金额上唯一完全匹配：STORE_F", rendered["text"])
        self.assertIn("2是", rendered["recommended_reply"])
        self.assertIsNone(gate["candidate_resolution"]["target"])

    def test_amount_hint_yes_is_explicit_human_confirmation(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = roi.RuntimePaths.for_workspace(workspace)
            decision = campaign_side()
            decision.update(
                source="FILE_A / FILE_B",
                sources=["FILE_A", "FILE_B"],
                mapping_sources=["full_store_export"],
                target_kind="store",
                useful_hint={
                    "evidence_status": "AMOUNT_ONLY_HINT",
                    "candidate": "STORE_F",
                    "selected_answer": None,
                },
            )
            gate = roi.resolution_gate(decision, "HG-06")
            batch = roi.make_review_batch([inferred_member("PLAN_A_1", entity_type="campaign")])
            batch["review_ux"] = review_ux.render_review_batch(batch, verified_records=1, human_gates=[gate])
            state = review_state(workspace, batch)
            state["gates"] = [gate]
            state["template_model"]["store_groups"].append({"store": "STORE_F", "products": ["PRODUCT_X"]})
            roi.atomic_json(paths.current_run, state)
            with patch("daily_roi_lib.run_report", side_effect=lambda *args, **kwargs: kwargs["existing_state"]):
                resolved = roi.resolve_review_batch(workspace, reply_text="全部接受；2是")
            self.assertEqual(resolved["gates"], [])
            self.assertEqual(resolved["run_mappings"]["campaign:fullstoreexport"], "STORE_F")
            self.assertEqual(roi.LocalMemory(paths).data["entity_mappings"], [])
            confirmation_lines = paths.confirmations.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("AMOUNT_ONLY_HINT" in line and "HUMAN_CONFIRMED" in line for line in confirmation_lines))


class ConsolidatedMemoryTests(unittest.TestCase):
    def setUp(self):
        self.decisions = [
            inferred_member("PRODUCT_ALIAS_A", entity_type="product"),
            inferred_member("PLAN_A_1", entity_type="campaign", sources=["PLAN_A_1", "PLAN_A_3"]),
            inferred_member("full_store_export", entity_type="campaign", run_only=True),
        ]

    def test_consolidated_accept_preserves_every_eligible_member(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = roi.RuntimePaths.for_workspace(workspace)
            batch = roi.make_review_batch(self.decisions)
            self.assertEqual(len(batch["items"]), 1)
            roi.atomic_json(paths.current_run, review_state(workspace, batch))
            with patch("daily_roi_lib.run_report", side_effect=lambda *args, **kwargs: kwargs["existing_state"]):
                roi.resolve_review_batch(workspace, reply_text="全部接受，符合长期记忆条件的映射记住")
            memory = roi.LocalMemory(paths)
            self.assertEqual(memory.resolve("product", "PRODUCT_ALIAS_A", scope={"store": "STORE_A"}), "PRODUCT_X")
            self.assertEqual(memory.resolve("campaign", "PLAN_A_1", scope={"store": "STORE_A"}), "PRODUCT_X")
            self.assertEqual(memory.resolve("campaign", "PLAN_A_3", scope={"store": "STORE_A"}), "PRODUCT_X")

    def test_consolidated_run_only_member_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = roi.RuntimePaths.for_workspace(workspace)
            batch = roi.make_review_batch(self.decisions)
            roi.atomic_json(paths.current_run, review_state(workspace, batch))
            with patch("daily_roi_lib.run_report", side_effect=lambda *args, **kwargs: kwargs["existing_state"]):
                roi.resolve_review_batch(workspace, reply_text="全部接受，符合长期记忆条件的映射记住")
            memory = roi.LocalMemory(paths)
            self.assertIsNone(memory.resolve("campaign", "full_store_export", scope={"store": "STORE_A"}))


if __name__ == "__main__":
    unittest.main()
