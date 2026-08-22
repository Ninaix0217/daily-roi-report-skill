from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import daily_roi  # noqa: E402
import daily_roi_lib as roi  # noqa: E402
import evidence_resolution as evidence  # noqa: E402
import test_candidate4_identity_provenance as candidate4  # noqa: E402


PRODUCT_A = candidate4.PRODUCT_A
PRODUCT_B = candidate4.PRODUCT_B
STORE = candidate4.STORE
STORE_B = "STORE_B"
REVIEW_SOURCE = "ALIAS_A"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def target_template_model() -> dict:
    model = candidate4.synthetic_template()
    model["store_groups"].append({"store": STORE_B, "products": []})
    return model


class TypedHumanGateProductionTests(unittest.TestCase):
    def _mixed_pending_state(
        self,
        workspace: Path,
        input_dir: Path,
        output_dir: Path,
        bridge,
    ):
        pending = roi.run_report(workspace, input_dir, output_dir)
        product_gate = next(
            gate for gate in pending["gates"]
            if ((gate.get("candidate_resolution") or {}).get("entity_type") == "product")
        )

        review_decision = evidence.resolve_entity(
            REVIEW_SOURCE,
            [PRODUCT_A, PRODUCT_B],
            entity_type="product",
            context_candidates=[PRODUCT_A, PRODUCT_B],
            cross_file_targets=[PRODUCT_A],
        )
        self.assertEqual(review_decision["decision"], evidence.INFERRED_REVIEW)
        review_decision.update(
            mapping_sources=[REVIEW_SOURCE],
            target_kind="product",
            business_relation_kind="PRODUCT_ASSIGNMENT",
        )
        review_batch = roi.make_review_batch([review_decision])

        store_decision = evidence.resolve_entity(
            "FILE_A",
            [STORE, STORE_B],
            entity_type="store",
            context_candidates=[],
        )
        store_decision.update(
            entity_type="campaign",
            source="FILE_A",
            sources=["FILE_A"],
            mapping_sources=["full_store_export"],
            target_kind="store",
            business_relation_kind="STORE_ALLOCATION",
            useful_hint={
                "evidence_status": "AMOUNT_ONLY_HINT",
                "candidate": STORE,
                "selected_answer": None,
            },
        )
        store_gate = roi.resolution_gate(store_decision, "HG-06")
        self.assertEqual(roi.human_gate_target_domain(store_gate), "STORE")
        self.assertEqual(roi.human_gate_target_domain(product_gate), "PRODUCT")

        pending["template_model"]["store_groups"].append({"store": STORE_B, "products": []})
        pending["review_batch"] = review_batch
        pending["gates"] = [store_gate, product_gate]
        pending["status"] = evidence.HUMAN_REQUIRED
        pending["stage"] = "RESOLVE"
        paths = roi.RuntimePaths.for_workspace(workspace)
        roi.atomic_json(paths.current_run, pending)
        review_path = paths.runs_dir / pending["run_id"] / "review-batch.json"
        roi.atomic_json(review_path, review_batch)
        return pending, paths, review_path, product_gate, store_gate

    @staticmethod
    def _reply(review_batch: dict, gates: list[dict], *, product_target: str) -> str:
        next_number = max(int(item["number"]) for item in review_batch["items"]) + 1
        numbers = {
            gate["gate_id"]: next_number + offset
            for offset, gate in enumerate(gates)
        }
        store_gate = next(
            gate for gate in gates
            if ((gate.get("candidate_resolution") or {}).get("target") is None)
        )
        product_gate = next(gate for gate in gates if gate is not store_gate)
        return (
            f"全部接受；{numbers[store_gate['gate_id']]}是；"
            f"{numbers[product_gate['gate_id']]}改为{product_target}"
        )

    @staticmethod
    def _review_cli(workspace: Path, reply: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            "daily_roi.py",
            "review",
            "--workspace",
            str(workspace),
            "--reply",
            reply,
        ]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = daily_roi.main()
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_mixed_review_store_confirmation_and_product_correction_is_atomic(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            harness = candidate4.IdentityProvenanceProductionTests()
            workspace, input_dir, output_dir, bridge, patches = harness._environment(root)
            with patches[0], patches[1], patches[2]:
                pending, paths, review_path, product_gate, store_gate = self._mixed_pending_state(
                    workspace,
                    input_dir,
                    output_dir,
                    bridge,
                )
                current_before = file_hash(paths.current_run)
                review_before = file_hash(review_path)
                reply = self._reply(pending["review_batch"], pending["gates"], product_target=PRODUCT_B)
                exit_code, _stdout, stderr = self._review_cli(workspace, reply)

                if exit_code != 0:
                    self.assertEqual(file_hash(paths.current_run), current_before)
                    self.assertEqual(file_hash(review_path), review_before)
                    self.assertFalse(paths.confirmations.exists())
                    self.assertFalse(paths.memory.exists())
                    bridge.write.assert_not_called()
                    self.fail(f"Candidate 4 mixed review RED: exit={exit_code}; stderr={stderr.strip()}")

                completed = json.loads(paths.current_run.read_text(encoding="utf-8"))
                confirmations = [
                    json.loads(line)
                    for line in paths.confirmations.read_text(encoding="utf-8").splitlines()
                ]
                memory_created = paths.memory.exists()
                written_payload = bridge.written_payload

        self.assertEqual(completed["run_id"], pending["run_id"])
        self.assertEqual(completed["status"], "COMPLETE")
        self.assertIsNone(completed["review_batch"])
        self.assertFalse(completed["gates"])
        self.assertFalse(memory_created)
        self.assertEqual(bridge.write.call_count, 1)
        self.assertEqual(len(confirmations), 3)
        review_confirmation = next(item for item in confirmations if item.get("review_id"))
        store_confirmation = next(item for item in confirmations if item.get("gate_id") == store_gate["gate_id"])
        product_confirmation = next(item for item in confirmations if item.get("gate_id") == product_gate["gate_id"])
        self.assertEqual(review_confirmation["action"], "ACCEPT")
        self.assertEqual(store_confirmation["action"], "CONFIRM_HINT")
        self.assertEqual(product_confirmation["action"], "CORRECT")
        self.assertEqual(product_confirmation["resolution"]["target"], PRODUCT_B)
        self.assertNotIn("HARD_IDENTITY", json.dumps(product_confirmation, ensure_ascii=False))
        expenses = {item["product"]: item for item in written_payload["product_expenses"]}
        self.assertEqual(expenses[PRODUCT_A]["components_cents"], [])
        self.assertEqual(expenses[PRODUCT_B]["components_cents"], [12345])
        self.assertEqual(written_payload["expense_total_cents"], 12345)
        self.assertEqual(completed["audit"]["expense_difference_cents"], 0)

    def test_invalid_product_in_mixed_batch_rejects_every_mutation(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            harness = candidate4.IdentityProvenanceProductionTests()
            workspace, input_dir, output_dir, bridge, patches = harness._environment(root)
            with patches[0], patches[1], patches[2]:
                pending, paths, review_path, _product_gate, _store_gate = self._mixed_pending_state(
                    workspace,
                    input_dir,
                    output_dir,
                    bridge,
                )
                current_before = file_hash(paths.current_run)
                review_before = file_hash(review_path)
                reply = self._reply(
                    pending["review_batch"],
                    pending["gates"],
                    product_target="UNKNOWN_PRODUCT",
                )
                exit_code, _stdout, stderr = self._review_cli(workspace, reply)

                self.assertEqual(exit_code, 1)
                self.assertIn("Human Gate PRODUCT target is not present", stderr)
                self.assertEqual(file_hash(paths.current_run), current_before)
                self.assertEqual(file_hash(review_path), review_before)
                self.assertFalse(paths.confirmations.exists())
                self.assertFalse(paths.memory.exists())
                self.assertFalse(output_dir.exists())
                bridge.write.assert_not_called()


class HumanGateTargetDomainTests(unittest.TestCase):
    def test_placement_source_product_assignment_uses_product_namespace(self):
        decision = evidence.resolve_entity(
            "PLACEMENT_NEW",
            [PRODUCT_A, PRODUCT_B],
            entity_type="product",
            semantic_candidates=[],
        )
        decision.update(
            entity_type="campaign",
            target_kind="product",
            business_relation_kind="PRODUCT_ASSIGNMENT",
            mapping_sources=["PLACEMENT_NEW"],
        )
        gate = roi.resolution_gate(decision, "HG-05")
        model = target_template_model()

        self.assertEqual(roi.human_gate_target_domain(gate), "PRODUCT")
        self.assertEqual(roi.validate_human_gate_target(gate, PRODUCT_B, model), PRODUCT_B)
        with self.assertRaisesRegex(roi.DailyRoiError, "Human Gate PRODUCT target"):
            roi.validate_human_gate_target(gate, "UNKNOWN_PRODUCT", model)
        with self.assertRaisesRegex(roi.DailyRoiError, "Human Gate PRODUCT target"):
            roi.validate_human_gate_target(gate, STORE, model)

    def test_store_assignment_uses_store_namespace_and_rejects_product(self):
        decision = evidence.resolve_entity(
            "FILE_A",
            [STORE, STORE_B],
            entity_type="store",
            context_candidates=[],
        )
        decision.update(
            entity_type="campaign",
            target_kind="store",
            business_relation_kind="STORE_ASSIGNMENT",
            mapping_sources=["FILE_A"],
        )
        gate = roi.resolution_gate(decision, "HG-06")
        model = target_template_model()

        self.assertEqual(roi.human_gate_target_domain(gate), "STORE")
        self.assertEqual(roi.validate_human_gate_target(gate, STORE, model), STORE)
        with self.assertRaisesRegex(roi.DailyRoiError, "Human Gate STORE target"):
            roi.validate_human_gate_target(gate, PRODUCT_A, model)

    def test_unknown_target_domain_fails_closed(self):
        gate = {
            "candidate_resolution": {
                "entity_type": "campaign",
                "sources": ["SOURCE_A"],
                "business_relation_kind": "UNKNOWN_ASSIGNMENT",
            },
            "evidence": {"resolution": {}},
        }
        with self.assertRaisesRegex(roi.DailyRoiError, "Unsupported Human Gate target domain"):
            roi.human_gate_target_domain(gate)


if __name__ == "__main__":
    unittest.main()
