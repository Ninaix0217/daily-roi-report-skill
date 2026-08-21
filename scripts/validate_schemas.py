#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from daily_roi_lib import LocalMemory, RuntimePaths, make_gate, make_review_batch
from evidence_resolution import INFERRED_REVIEW, finalize_resolution


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"


def review_decision() -> dict:
    return finalize_resolution({
        "schema_version": 1,
        "entity_type": "campaign",
        "source": "脱敏计划1",
        "sources": ["脱敏计划1", "脱敏计划3"],
        "fact_family": "campaign:脱敏计划",
        "candidate": "脱敏产品",
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
    })


def main() -> int:
    schemas = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in sorted(SCHEMA_DIR.glob("*.schema.json"))}
    registry = Registry()
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))

    decision = review_decision()
    batch = make_review_batch([decision])
    gate = make_gate("HG-06", "sanitized unresolved fact", {"fixture": True}, "请选择脱敏模板产品。")
    with tempfile.TemporaryDirectory() as root:
        local_memory = LocalMemory(RuntimePaths.for_workspace(Path(root)))
        local_memory.add_mapping(
            "campaign", "脱敏计划1", "脱敏产品", gate_id="RV-schema",
            confirmation_mode="REVIEW_ACCEPT", original_proposal="脱敏产品",
            evidence_at_confirmation=["sanitized schema instance"], source_run="schema-validation",
        )
        memory = local_memory.data
        instances = {
            "memory.schema.json": memory,
            "evidence-resolution.schema.json": decision,
            "review-batch.schema.json": batch,
            "human-gate.schema.json": gate,
            "run-state.schema.json": {
                "schema_version": 1,
                "run_id": "schema-validation",
                "status": "INFERRED_REVIEW",
                "stage": "RESOLVE",
                "input_dir": "sanitized/input",
                "output_dir": "sanitized/output",
                "manifest": [],
                "gates": [],
                "review_batch": batch,
                "review_metrics": {"rejected_proposals_persisted_as_fact": 0},
                "run_mappings": {},
                "audit": {"resolutions": [decision], "resolution_summary": {}},
            },
        }
        for name, instance in instances.items():
            Draft202012Validator(schemas[name], registry=registry).validate(instance)
    print(json.dumps({
        "SCHEMAS_CHECKED": len(schemas),
        "RUNTIME_INSTANCES_VALIDATED": len(instances),
        "SCHEMA_VALIDATION": "PASS",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
