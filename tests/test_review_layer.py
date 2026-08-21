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
    atomic_json,
    make_review_batch,
    resolve_review_batch,
)
from evidence_resolution import (  # noqa: E402
    HUMAN_REQUIRED,
    INFERRED_REVIEW,
    VERIFIED,
    finalize_resolution,
    resolve_entity,
    resolve_global_store_constraints,
)


def inferred(source: str, target: str, family: str | None = None) -> dict:
    return finalize_resolution({
        "schema_version": 1,
        "entity_type": "campaign",
        "source": source,
        "sources": [source],
        "fact_family": family or f"campaign:{source}",
        "candidate": target,
        "decision": INFERRED_REVIEW,
        "evidence": [
            {"type": "shared_semantic_anchor", "anchor": source[:1]},
            {"type": "current_store_product_scope", "candidate_count": 1},
            {"type": "unique_candidate_in_current_template"},
            {"type": "reconciliation_consistent", "difference_cents": 0},
        ],
        "contradictions": [],
        "alternatives": [],
        "candidate_generation": [{
            "candidate": target,
            "evidence": [{"type": "shared_semantic_anchor"}],
            "source_scope": "current_store_products",
            "reason": "sanitized_test_fixture",
        }],
        "reconciliation": {"status": "PASS", "difference_cents": 0},
    })


def state_for_batch(workspace: Path, batch: dict) -> dict:
    return {
        "schema_version": 1,
        "run_id": "review-run",
        "status": INFERRED_REVIEW,
        "stage": "RESOLVE",
        "created_at": "2026-01-01T00:00:00+00:00",
        "input_dir": str(workspace / "input"),
        "output_dir": str(workspace / "output"),
        "manifest": [],
        "template_model": {
            "report": {"products": [{"name": "净润液"}, {"name": "净护液"}, {"name": "清护水"}, {"name": "舒缓膏"}]},
            "store_groups": [],
        },
        "gates": [],
        "review_batch": batch,
        "review_metrics": {},
        "run_mappings": {},
    }


class EvidenceClassificationTests(unittest.TestCase):
    def test_r1_hard_identity_is_verified_without_review(self):
        decision = resolve_entity(
            "SKU-001",
            ["Product A"],
            entity_type="product",
            exact_matches=[("Product A", "exact_template_sku", {"sku": "SKU-001"})],
        )
        self.assertEqual(decision["decision"], VERIFIED)
        self.assertIn("HARD_IDENTITY", decision["evidence_classes"])
        self.assertFalse(decision["human_review_required"])
        self.assertIsNone(make_review_batch([decision]))

    def test_r2_strong_semantic_inference_proposes_answer_for_review(self):
        decision = resolve_entity(
            "净1",
            ["净润液", "Other Product"],
            entity_type="campaign",
            context_candidates=["净润液"],
            sibling_sources=["净1", "净3"],
            reconciliation={"status": "PASS", "difference_cents": 0},
        )
        batch = make_review_batch([decision])
        self.assertEqual(decision["decision"], INFERRED_REVIEW)
        self.assertEqual(batch["items"][0]["proposed_answer"], "净润液")
        self.assertIn("是否接受", batch["items"][0]["question"])
        self.assertNotIn("是什么", batch["items"][0]["question"])

    def test_r3_two_supported_candidates_remain_human_required(self):
        decision = resolve_entity(
            "暖舒贴",
            ["暖适贴", "舒暖贴"],
            entity_type="campaign",
            context_candidates=["暖适贴", "舒暖贴"],
        )
        self.assertEqual(decision["decision"], HUMAN_REQUIRED)
        self.assertEqual(set(decision["alternatives"]), {"暖适贴", "舒暖贴"})

    def test_r7_global_reconciliation_is_review_not_fact_or_open_question(self):
        result = resolve_global_store_constraints(
            [
                {"id": "a", "source": "a.csv", "total_cents": 101, "candidate_stores": ["Store A"], "products": ["Product A"]},
                {"id": "b", "source": "b.csv", "total_cents": 202, "candidate_stores": ["Store A"], "products": ["Product A"]},
            ],
            {"Store A": 303},
        )
        self.assertEqual(result["status"], INFERRED_REVIEW)
        self.assertEqual({item["decision"] for item in result["decisions"].values()}, {INFERRED_REVIEW})
        self.assertIn("GLOBAL_RECONCILIATION", next(iter(result["decisions"].values()))["evidence_classes"])

    def test_r8_hard_identity_contradiction_still_requires_human(self):
        decision = resolve_entity(
            "ID-1",
            ["Product A", "Product B"],
            entity_type="product",
            exact_matches=[("Product A", "exact_template_product_identity", {"identity": "ID-1"})],
            context_candidates=["Product B"],
        )
        self.assertEqual(decision["decision"], HUMAN_REQUIRED)
        self.assertEqual(decision["contradictions_checked"]["status"], "FAIL")
        self.assertNotEqual(decision.get("candidate"), "Product B")


class ReviewLifecycleTests(unittest.TestCase):
    def test_r4_accept_upgrades_and_persists_then_resumes(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = RuntimePaths.for_workspace(workspace)
            batch = make_review_batch([inferred("净1", "净润液")])
            atomic_json(paths.current_run, state_for_batch(workspace, batch))
            response = [{
                "review_id": batch["items"][0]["review_id"],
                "action": "ACCEPT",
                "persistence": "PERSISTENT_REUSABLE",
            }]
            with patch("daily_roi_lib.run_report", side_effect=lambda *args, **kwargs: kwargs["existing_state"]) as resumed:
                result = resolve_review_batch(workspace, response)
            self.assertEqual(LocalMemory(paths).resolve("campaign", "净1"), "净润液")
            self.assertEqual(result["review_metrics"]["review_accept_count"], 1)
            resumed.assert_called_once()
            confirmation = json.loads(paths.confirmations.read_text(encoding="utf-8").strip())
            self.assertEqual(confirmation["decision_type"], "HUMAN_CONFIRMED")
            reused = resolve_entity(
                "净1",
                ["净润液"],
                entity_type="campaign",
                exact_matches=[("净润液", "human_confirmed_local_mapping", None)],
            )
            self.assertEqual(reused["confirmation_provenance"], "HUMAN_CONFIRMED")

    def test_r5_reject_and_correct_never_persists_proposal(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = RuntimePaths.for_workspace(workspace)
            batch = make_review_batch([inferred("净1", "净润液")])
            atomic_json(paths.current_run, state_for_batch(workspace, batch))
            response = [{
                "review_id": batch["items"][0]["review_id"],
                "action": "CORRECT",
                "target": "净护液",
                "persistence": "PERSISTENT_REUSABLE",
            }]
            with patch("daily_roi_lib.run_report", side_effect=lambda *args, **kwargs: kwargs["existing_state"]):
                result = resolve_review_batch(workspace, response)
            memory = LocalMemory(paths)
            self.assertEqual(memory.resolve("campaign", "净1"), "净护液")
            self.assertNotEqual(memory.resolve("campaign", "净1"), "净润液")
            self.assertEqual(result["review_metrics"]["review_correct_count"], 1)
            confirmation = json.loads(paths.confirmations.read_text(encoding="utf-8").strip())
            self.assertTrue(confirmation["rejected"])
            self.assertEqual(confirmation["proposal"], "净润液")
            self.assertEqual(confirmation["final_answer"], "净护液")

    def test_r6_three_decisions_are_one_batch_and_one_resume(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = RuntimePaths.for_workspace(workspace)
            batch = make_review_batch([
                inferred("净1", "净润液"),
                inferred("清1", "清护水"),
                inferred("舒1", "舒缓膏"),
            ])
            self.assertEqual(len(batch["items"]), 3)
            atomic_json(paths.current_run, state_for_batch(workspace, batch))
            with patch("daily_roi_lib.run_report", side_effect=lambda *args, **kwargs: kwargs["existing_state"]) as resumed:
                result = resolve_review_batch(workspace, accept_all=True)
            self.assertEqual(result["review_metrics"]["review_accept_count"], 3)
            resumed.assert_called_once()

    def test_batch_validation_fails_before_any_partial_persistence(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = RuntimePaths.for_workspace(workspace)
            global_result = resolve_global_store_constraints(
                [{"id": "a", "source": "a.csv", "total_cents": 100, "candidate_stores": ["Store A"], "products": ["Product A"]}],
                {"Store A": 100},
            )
            batch = make_review_batch([inferred("净1", "净润液"), next(iter(global_result["decisions"].values()))])
            atomic_json(paths.current_run, state_for_batch(workspace, batch))
            with self.assertRaisesRegex(Exception, "not eligible for durable memory"):
                resolve_review_batch(workspace, accept_all=True, default_persistence="PERSISTENT_REUSABLE")
            self.assertIsNone(LocalMemory(paths).resolve("campaign", "净1"))
            state = json.loads(paths.current_run.read_text(encoding="utf-8"))
            self.assertEqual(state["run_mappings"], {})
            self.assertFalse(paths.confirmations.exists())

    def test_reject_never_persists_proposal(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = RuntimePaths.for_workspace(workspace)
            batch = make_review_batch([inferred("净1", "净润液")])
            atomic_json(paths.current_run, state_for_batch(workspace, batch))
            response = [{"review_id": batch["items"][0]["review_id"], "action": "REJECT"}]
            with patch("daily_roi_lib.run_report", side_effect=lambda *args, **kwargs: kwargs["existing_state"]) as resumed:
                state = resolve_review_batch(workspace, response)
            self.assertIsNone(LocalMemory(paths).resolve("campaign", "净1"))
            self.assertEqual(state["review_metrics"]["review_reject_count"], 1)
            self.assertTrue(all(state["run_mappings"][key] == "rejected" for key in batch["items"][0]["member_keys"]))
            resumed.assert_called_once()


class Rc3KnownFailureArchetypeTests(unittest.TestCase):
    def test_rc3_inspired_simulated_archetypes_reclassify_to_three_three_one(self):
        fixture = json.loads((ROOT / "evals" / "fixtures" / "review-layer" / "rc3-known-failure-archetypes.json").read_text(encoding="utf-8"))
        self.assertEqual(fixture["evidence_class"], "SIMULATED")
        self.assertEqual(fixture["historical_basis"], "REAL_OBSERVED_FAILURE_SHAPE")
        self.assertFalse(fixture["original_run_retained"])
        decisions = [
            finalize_resolution({
                "schema_version": 1,
                "entity_type": "workflow",
                "source": "prior-template-date",
                "sources": ["prior-template-date"],
                "fact_family": "workflow:stale-template-date",
                "candidate": "current-business-date",
                "decision": VERIFIED,
                "evidence": [
                    {"type": "consistent_business_dates_template_only_stale"},
                    {"type": "human_confirmed_workflow_rule"},
                ],
                "contradictions": [],
                "alternatives": [],
                "candidate_generation": [],
                "reconciliation": {"status": "PASS"},
            }),
            resolve_entity("SKU-1", ["Product A"], entity_type="product", exact_matches=[("Product A", "exact_template_sku", None)]),
            resolve_entity("单盒", ["Main Product"], entity_type="campaign", exact_matches=[("Main Product", "generic_plan_to_unique_store_main_product", None)]),
        ]
        for family in fixture["semantic_families"]:
            decisions.append(resolve_entity(
                family["sources"][0],
                [family["candidate"]],
                entity_type="campaign",
                context_candidates=[family["candidate"]],
                sibling_sources=family["sources"],
                reconciliation={"status": "PASS", "difference_cents": 0},
            ))
        global_result = resolve_global_store_constraints(fixture["global"]["files"], fixture["global"]["ledger_totals"])
        decisions.append(next(iter(global_result["decisions"].values())))
        decisions.append(resolve_entity("暖舒贴", ["暖适贴", "舒暖贴"], entity_type="campaign", context_candidates=["暖适贴", "舒暖贴"]))
        counts = {
            "verified": sum(item["decision"] == VERIFIED for item in decisions),
            "inferred_review": sum(item["decision"] == INFERRED_REVIEW for item in decisions),
            "human_required": sum(item["decision"] == HUMAN_REQUIRED for item in decisions),
            "open_ended_human_decisions": sum(item["decision"] == HUMAN_REQUIRED for item in decisions),
        }
        self.assertEqual(len(decisions), fixture["scenario_items"])
        self.assertEqual(counts, fixture["expected"])


if __name__ == "__main__":
    unittest.main()
