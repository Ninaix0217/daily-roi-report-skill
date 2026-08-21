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
    evaluate_dates,
    make_review_batch,
    resolve_product_evidence,
    resolve_review_batch,
)
from evidence_resolution import (  # noqa: E402
    HUMAN_REQUIRED,
    INFERRED_REVIEW,
    finalize_resolution,
    merge_inferred_decisions,
    merge_human_decisions,
    resolve_entity,
    resolve_global_store_constraints,
)
from review_ux import ReviewReplyError, parse_review_reply, persistence_eligibility, render_review_batch  # noqa: E402
from review_archetype_scenarios import evaluate_scenarios  # noqa: E402


def inferred(source: str, target: str, *, family: str | None = None, contexts: list[dict] | None = None) -> dict:
    return finalize_resolution({
        "schema_version": 1,
        "entity_type": "campaign",
        "source": source,
        "sources": [source],
        "fact_family": family or f"campaign:{source}",
        "candidate": target,
        "decision": INFERRED_REVIEW,
        "evidence": [
            {"type": "normalized_containment"},
            {"type": "current_store_product_scope"},
            {"type": "unique_candidate_in_current_template"},
        ],
        "contradictions": [],
        "alternatives": [],
        "candidate_generation": [],
        "reconciliation": {"status": "PASS"},
        "contexts": contexts or [],
    })


def state_for(workspace: Path, batch: dict, products: list[str], stores: list[str] | None = None) -> dict:
    return {
        "schema_version": 1,
        "run_id": "sanitized-v12-run",
        "status": INFERRED_REVIEW,
        "stage": "RESOLVE",
        "created_at": "2026-08-21T00:00:00+08:00",
        "input_dir": str(workspace / "input"),
        "output_dir": str(workspace / "output"),
        "manifest": [],
        "template_model": {
            "report": {"products": [{"name": value} for value in products]},
            "store_groups": [{"store": value, "products": products} for value in (stores or [])],
        },
        "gates": [],
        "review_batch": batch,
        "review_metrics": {},
        "run_mappings": {},
    }


class EvidenceResolutionV12Tests(unittest.TestCase):
    def test_00_review_v2_simulated_archetypes_match_declared_metrics(self):
        fixture_path = ROOT / "evals" / "fixtures" / "review-layer" / "review-v2-archetype-scenarios.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        result = evaluate_scenarios(fixture_path)
        self.assertEqual(fixture["evidence_class"], "SIMULATED")
        self.assertEqual(fixture["historical_basis"], "REAL_OBSERVED_FAILURE_SHAPE")
        self.assertFalse(fixture["original_run_retained"])
        self.assertFalse(fixture["contains_private_business_data"])
        expected = fixture["expected"]
        for key, value in expected.items():
            self.assertEqual(result[key.upper()], value)

    def test_01_stale_template_auto_update_is_shared_core(self):
        with tempfile.TemporaryDirectory() as root:
            memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
            target, update, gates = evaluate_dates({"financial": ["2026-08-19"], "campaign": ["2026-08-19"]}, "2026-08-18", memory)
            self.assertEqual((target, update, gates), ("2026-08-19", True, []))
            self.assertEqual(memory.data["workflow_rules"], [])

    def test_02_conflicting_business_dates_require_human(self):
        with tempfile.TemporaryDirectory() as root:
            result = evaluate_dates({"financial": ["2026-08-19"], "campaign": ["2026-08-18"]}, "2026-08-18", LocalMemory(RuntimePaths.for_workspace(Path(root))))
            self.assertFalse(result[1])
            self.assertEqual([item["gate_type"] for item in result[2]], ["HG-02"])

    def test_03_semantic_siblings_consolidate_to_one_business_decision(self):
        items = [
            inferred("青木计划", "青木液", family="campaign:青木计划"),
            inferred("青木1", "青木液", family="campaign:青木"),
            inferred("青木3", "青木液", family="campaign:青木"),
        ]
        merged = merge_inferred_decisions(items)
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0]["sources"]), {"青木计划", "青木1", "青木3"})

    def test_03b_unrelated_aliases_with_same_target_remain_independent(self):
        merged = merge_inferred_decisions([inferred("甲计划", "同一产品"), inferred("乙渠道", "同一产品")])
        self.assertEqual(len(merged), 2)

    def test_03c_rejected_semantic_siblings_stay_one_open_decision(self):
        items = []
        for source, family in (("青木计划", "campaign:青木计划"), ("青木1", "campaign:青木"), ("青木3", "campaign:青木")):
            item = inferred(source, "青木液", family=family)
            item.update(decision=HUMAN_REQUIRED, candidate=None, alternatives=["青木液"])
            items.append(finalize_resolution(item))
        self.assertEqual(len(merge_human_decisions(items)), 1)

    def test_04_short_chinese_token_rejects_single_character_typo_candidate(self):
        decision = resolve_entity(
            "青1", ["青木液", "白石膏", "其他产品"], entity_type="campaign",
            context_candidates=["青木液", "白石膏", "其他产品"],
            sibling_sources=["青1", "青3"], reconciliation={"status": "PASS"},
        )
        self.assertEqual((decision["decision"], decision["candidate"]), (INFERRED_REVIEW, "青木液"))
        generated = {item["candidate"] for item in decision["candidate_generation"]}
        self.assertNotIn("白石膏", generated)

    def test_05_global_unique_complete_explanation_is_review(self):
        result = resolve_global_store_constraints([
            {"id": "a", "source": "site-a.csv", "total_cents": 11111, "candidate_stores": ["Example Store-A"], "products": ["Example Product-A"]},
            {"id": "b", "source": "store-b.csv", "total_cents": 22222, "candidate_stores": ["Example Store-A"], "products": ["Example Product-A"]},
        ], {"Example Store-A": 33333})
        self.assertEqual(result["status"], INFERRED_REVIEW)
        self.assertEqual(set(result["assignments"].values()), {"Example Store-A"})
        self.assertEqual({item["review_risk"] for item in result["decisions"].values()}, {"MEDIUM_REVIEW_RISK"})

    def test_06_global_ambiguous_allocation_requires_human(self):
        result = resolve_global_store_constraints([
            {"id": "a", "source": "a.csv", "total_cents": 100, "candidate_stores": ["A", "B"], "products": ["P"]},
            {"id": "b", "source": "b.csv", "total_cents": 100, "candidate_stores": ["A", "B"], "products": ["P"]},
        ], {"A": 100, "B": 100})
        self.assertEqual(result["status"], HUMAN_REQUIRED)
        self.assertEqual(result["solutions_found"], 2)

    def test_07_supported_multi_candidate_has_preferred_review_answer(self):
        decision = resolve_entity("新蓝叶器", ["蓝叶器", "蓝叶贴"], entity_type="campaign")
        self.assertEqual((decision["decision"], decision["candidate"]), (INFERRED_REVIEW, "蓝叶器"))
        self.assertEqual(decision["alternatives"], ["蓝叶贴"])

    def test_08_no_justified_preference_remains_human_required(self):
        decision = resolve_entity("暖舒贴", ["暖适贴", "舒暖贴"], entity_type="campaign", context_candidates=["暖适贴", "舒暖贴"])
        self.assertEqual(decision["decision"], HUMAN_REQUIRED)


class ReviewUxV2Tests(unittest.TestCase):
    def setUp(self):
        self.batch = make_review_batch([
            inferred("青1", "青木液"),
            inferred("新蓝叶器", "蓝叶器"),
            inferred("松1", "松露膏"),
            inferred("云1", "云纱液"),
        ])
        device_item = next(item for item in self.batch["items"] if "新蓝叶器" in item["sources"])
        device_item["alternatives"] = ["蓝叶贴"]
        self.batch["review_ux"] = render_review_batch(self.batch, verified_records=5)

    def test_09_independent_decision_consolidation(self):
        batch = make_review_batch([inferred("松露", "松露膏"), inferred("松露1", "松露膏"), inferred("松露3", "松露膏")])
        self.assertEqual(len(batch["items"]), 1)
        self.assertEqual(set(batch["items"][0]["sources"]), {"松露", "松露1", "松露3"})

    def test_10_render_prefills_answers_and_risk_sections(self):
        rendered = self.batch["review_ux"]
        self.assertIn("自动确认：5 项", rendered["text"])
        self.assertIn("→ 建议：蓝叶器", rendered["text"])
        self.assertIn("### 建议重点确认", rendered["text"])
        self.assertEqual(rendered["prefilled_review_rate"], 1)

    def test_11_copyable_recommended_reply_is_plain_text(self):
        reply = self.batch["review_ux"]["recommended_reply"]
        self.assertEqual(reply, "全部接受，符合长期记忆条件的映射记住")
        self.assertNotIn("{", reply)

    def test_12_all_accept_parser_is_current_batch_only_and_run_only(self):
        parsed = parse_review_reply("全部接受", self.batch)
        self.assertEqual(len(parsed["responses"]), len(self.batch["items"]))
        self.assertEqual({item["persistence"] for item in parsed["responses"]}, {"RUN_ONLY"})

    def test_13_all_accept_with_one_correction_parser(self):
        parsed = parse_review_reply("全部接受，但2改为蓝叶贴", self.batch)
        corrected = next(item for item in parsed["responses"] if item["action"] == "CORRECT")
        self.assertEqual((corrected["action"], corrected["target"]), ("CORRECT", "蓝叶贴"))

    def test_14_mixed_accept_correct_parser(self):
        parsed = parse_review_reply("1、2、4对，3改为松露软膏", self.batch)
        self.assertEqual([item["action"] for item in parsed["responses"]].count("CORRECT"), 1)

    def test_15_partial_reply_does_not_break_batch_atomicity(self):
        with self.assertRaises(ReviewReplyError):
            parse_review_reply("1、2对", self.batch)


class MemoryLayerV2Tests(unittest.TestCase):
    def test_16_durable_entity_mapping_is_eligible(self):
        item = make_review_batch([inferred("青1", "青木液", contexts=[{"store": "Store A"}])])["items"][0]
        self.assertTrue(persistence_eligibility(item)["eligible"])
        self.assertEqual(persistence_eligibility(item)["memory_type"], "PLAN_PATTERN")
        self.assertEqual(persistence_eligibility(item)["scope"], {"store": "Store A"})

    def test_17_run_only_global_allocation_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = RuntimePaths.for_workspace(workspace)
            global_result = resolve_global_store_constraints([
                {"id": "a", "source": "全站.csv", "total_cents": 100, "candidate_stores": ["Store A"], "products": ["P"]}
            ], {"Store A": 100})
            batch = make_review_batch(global_result["decisions"].values())
            atomic_json(paths.current_run, state_for(workspace, batch, ["P"], ["Store A"]))
            with patch("daily_roi_lib.run_report", side_effect=lambda *args, **kwargs: kwargs["existing_state"]):
                result = resolve_review_batch(workspace, reply_text="全部接受，能记的记住")
            self.assertEqual(LocalMemory(paths).data["entity_mappings"], [])
            self.assertEqual(result["review_metrics"]["run_only_not_persisted"], 1)

    def test_18_store_scoped_mapping_does_not_leak(self):
        with tempfile.TemporaryDirectory() as root:
            memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
            memory.add_mapping("campaign", "青1", "青木液", gate_id="RV-1", scope={"store": "Store A"})
            self.assertEqual(memory.resolve("campaign", "青1", scope={"store": "Store A"}), "青木液")
            self.assertIsNone(memory.resolve("campaign", "青1", scope={"store": "Store B"}))

    def test_19_current_hard_identity_conflicts_with_memory(self):
        decision = resolve_entity(
            "Alias", ["Current", "Historical"], entity_type="product",
            exact_matches=[
                ("Current", "exact_template_product_identity", {"identity": "ID-1"}),
                ("Historical", "human_confirmed_local_mapping", {"lineage_id": "RV-old"}),
            ],
        )
        self.assertEqual(decision["decision"], HUMAN_REQUIRED)
        self.assertIn("memory_conflict", {item["type"] for item in decision["contradictions"]})

    def test_19b_runtime_conflict_marks_historical_memory_conflicted(self):
        with tempfile.TemporaryDirectory() as root:
            memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
            memory.add_mapping("campaign", "Alias", "Historical", gate_id="RV-old")
            model = {"report": {"products": [{"name": "Current"}, {"name": "Historical"}]}, "sku": {"map": {}, "conflicts": []}}
            decision = resolve_product_evidence(
                "Alias", model, memory, {}, identity_values={"sku": "ID-1"},
                identity_index={"ID-1": [{"product": "Current", "identity_type": "sku", "origin": "sanitized"}]},
            )
            self.assertEqual(decision["decision"], HUMAN_REQUIRED)
            self.assertEqual(memory.data["entity_mappings"][0]["status"], "CONFLICTED")

    def test_20_memory_never_silently_overrides_hard_identity(self):
        decision = resolve_entity(
            "Alias", ["Current", "Historical"], entity_type="product",
            exact_matches=[("Current", "exact_template_sku", None), ("Historical", "human_confirmed_local_mapping", None)],
        )
        self.assertEqual(decision["candidate"], "Current")
        self.assertNotEqual(decision["candidate"], "Historical")

    def test_21_review_accept_provenance_is_retained(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = RuntimePaths.for_workspace(workspace)
            batch = make_review_batch([inferred("青1", "青木液")])
            atomic_json(paths.current_run, state_for(workspace, batch, ["青木液"]))
            with patch("daily_roi_lib.run_report", side_effect=lambda *args, **kwargs: kwargs["existing_state"]):
                resolve_review_batch(workspace, reply_text="全部接受并记住")
            item = LocalMemory(paths).find("campaign", "青1")
            self.assertEqual(item["confirmation_mode"], "REVIEW_ACCEPT")
            self.assertEqual(item["original_proposal"], "青木液")

    def test_22_human_correction_supersedes_old_mapping(self):
        with tempfile.TemporaryDirectory() as root:
            memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
            old = memory.add_mapping("campaign", "A", "B", gate_id="RV-old")
            new = memory.add_mapping("campaign", "A", "C", gate_id="RV-new", confirmation_mode="HUMAN_CORRECTION", original_proposal="B", supersede=True)
            self.assertEqual(old["status"], "SUPERSEDED")
            self.assertEqual(new["status"], "ACTIVE")
            self.assertEqual(new["supersedes"], old["memory_id"])

    def test_23_reject_only_creates_audit_not_mapping(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            paths = RuntimePaths.for_workspace(workspace)
            batch = make_review_batch([inferred("A", "B")])
            atomic_json(paths.current_run, state_for(workspace, batch, ["B"]))
            with patch("daily_roi_lib.run_report", side_effect=lambda *args, **kwargs: kwargs["existing_state"]):
                resolve_review_batch(workspace, [{"review_id": batch["items"][0]["review_id"], "action": "REJECT"}])
            memory = LocalMemory(paths)
            self.assertEqual(memory.data["entity_mappings"], [])
            self.assertFalse(memory.data["rejected_proposals"][0]["creates_business_fact"])

    def test_24_superseded_memory_is_not_reused(self):
        with tempfile.TemporaryDirectory() as root:
            memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
            memory.add_mapping("product", "A", "B", gate_id="old")
            memory.add_mapping("product", "A", "C", gate_id="new", supersede=True)
            self.assertEqual(memory.resolve("product", "A"), "C")

    def test_24b_missing_current_target_retires_memory_without_ttl(self):
        with tempfile.TemporaryDirectory() as root:
            memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
            memory.add_mapping("product", "A", "Removed Target", gate_id="old")
            self.assertIsNone(memory.resolve("product", "A", valid_targets=["Current Target"]))
            self.assertEqual(memory.data["entity_mappings"][0]["status"], "RETIRED")
            self.assertEqual(memory.data["entity_mappings"][0]["retired_reason"], "target_absent_from_current_template")

    def test_25_same_lineage_evidence_is_not_double_counted(self):
        decision = resolve_entity(
            "A", ["B"], entity_type="product",
            exact_matches=[
                ("B", "human_confirmed_local_mapping", {"lineage_id": "RV-1", "memory_id": "MEM-1"}),
                ("B", "human_confirmed_local_mapping", {"lineage_id": "RV-1", "memory_id": "MEM-1-copy"}),
            ],
        )
        hits = [item for item in decision["evidence"] if item["type"] == "human_confirmed_local_mapping"]
        self.assertEqual(len(hits), 1)


if __name__ == "__main__":
    unittest.main()
