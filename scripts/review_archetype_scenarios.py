#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from daily_roi_lib import make_review_batch
from evidence_resolution import INFERRED_REVIEW, finalize_resolution, resolve_global_store_constraints
from review_ux import render_review_batch


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "evals" / "fixtures" / "review-layer" / "review-v2-archetype-scenarios.json"


def _review_decision(group: dict[str, Any]) -> dict[str, Any]:
    evidence = [
        {"type": "normalized_containment"},
        {"type": "sibling_alias_family", "sources": group["sources"]},
        {"type": "current_store_product_scope"},
        {"type": "unique_candidate_in_current_template"},
        {"type": "reconciliation_consistent"},
    ]
    alternatives = [group["alternative"]] if group.get("alternative") else []
    return finalize_resolution({
        "schema_version": 1,
        "entity_type": "campaign",
        "source": group["sources"][0],
        "sources": group["sources"],
        "fact_family": "simulated:" + group["candidate"],
        "business_decision_key": "simulated:" + group["candidate"],
        "candidate": group["candidate"],
        "decision": INFERRED_REVIEW,
        "evidence": evidence,
        "contradictions": [],
        "alternatives": alternatives,
        "candidate_generation": [],
        "reconciliation": {"status": "PASS"},
    })


def evaluate_scenarios(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    decisions = [_review_decision(group) for group in fixture["review_groups"]]
    global_result = resolve_global_store_constraints(fixture["global"]["files"], fixture["global"]["ledger_totals"])
    decisions.extend(global_result["decisions"].values())
    batch = make_review_batch(decisions)
    batch["review_ux"] = render_review_batch(
        batch,
        verified_records=int(fixture["render_context"]["verified_records"]),
    )
    items = list(batch["items"])
    prefilled = sum(bool(item.get("proposed_answer")) for item in items)
    eligible = sum(bool(item.get("persistence_candidate")) for item in items)
    run_only = sum(not bool(item.get("persistence_candidate")) for item in items)
    return {
        "FIXTURE_CLASS": fixture["fixture_class"],
        "EVIDENCE_CLASS": fixture["evidence_class"],
        "HISTORICAL_BASIS": fixture["historical_basis"],
        "ORIGINAL_RUN_RETAINED": fixture["original_run_retained"],
        "PRIVATE_DATA_PRESENT": fixture["contains_private_business_data"],
        "INDEPENDENT_REVIEW_DECISIONS": len(items),
        "PREFILLED_REVIEW_DECISIONS": prefilled,
        "PREFILLED_REVIEW_RATE": prefilled / len(items) if items else 0,
        "OPEN_ENDED_HUMAN_DECISIONS": sum(not bool(item.get("proposed_answer")) for item in items),
        "MEMORY_CANDIDATES": eligible,
        "DURABLE_MEMORY_ELIGIBLE": eligible,
        "RUN_ONLY_DECISIONS": run_only,
        "INFERRED_REVIEW": [
            {"number": item["number"], "sources": item["sources"], "proposed": item["proposed_answer"], "alternatives": item["alternatives"]}
            for item in items
        ],
        "REVIEW_TEXT": batch["review_ux"]["text"],
    }


def main() -> int:
    print(json.dumps(evaluate_scenarios(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
