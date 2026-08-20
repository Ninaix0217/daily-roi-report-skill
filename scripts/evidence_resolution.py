from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable


VERIFIED = "VERIFIED"
INFERRED_REVIEW = "INFERRED_REVIEW"
MACHINE_INFERRED = "MACHINE_INFERRED"
HUMAN_REQUIRED = "HUMAN_REQUIRED"

HARD_IDENTITY = "HARD_IDENTITY"
DETERMINISTIC_STRUCTURE = "DETERMINISTIC_STRUCTURE"
SEMANTIC_EVIDENCE = "SEMANTIC_EVIDENCE"
CROSS_FILE_EVIDENCE = "CROSS_FILE_EVIDENCE"
GLOBAL_RECONCILIATION = "GLOBAL_RECONCILIATION"
EXCLUSIVITY_EVIDENCE = "EXCLUSIVITY_EVIDENCE"

_HARD_IDENTITY_TYPES = {
    "exact_template_sku", "exact_template_product_identity", "human_confirmed_local_mapping",
    "confirmed_run_mapping", "human_confirmed_store_assignment", "human_confirmed_review",
    "human_confirmed_workflow_rule", "conflicting_template_sku",
}
_DETERMINISTIC_STRUCTURE_TYPES = {
    "exact_template_product", "exact_template_store", "exact_template_product_after_spec_normalization",
    "unique_store_product_membership", "generic_plan_to_unique_store_main_product", "current_store_product_scope",
    "consistent_business_dates_template_only_stale",
}
_SEMANTIC_TYPES = {
    "normalized_exact_name", "product_form_root_exact", "normalized_containment", "short_semantic_stem",
    "single_character_root_variant", "transposed_root_variant", "shared_semantic_anchor", "weak_shared_anchor",
    "same_product_form", "shared_distinct_token", "unique_shared_token", "sibling_alias_family",
}
_CROSS_FILE_TYPES = {"cross_file_identity_corroboration", "current_run_evidence_only"}
_GLOBAL_RECONCILIATION_TYPES = {
    "global_constraint_unique_solution", "current_file_amount_cents",
    "ledger_remainder_exact_across_current_files", "reconciliation_consistent",
}
_EXCLUSIVITY_TYPES = {
    "unique_candidate_in_current_template", "weaker_semantic_candidates_rejected",
    "candidate_store_scope", "unique_exact_target",
}

_VARIANT_SUFFIX = re.compile(r"(?:单盒|三盒|一盒|[13]盒|单|[13])$", re.I)
_FORM_TOKENS = tuple(
    sorted(
        {
            "吸吸棒",
            "贴膏",
            "软膏",
            "喷雾",
            "胶囊",
            "眼贴",
            "吸棒",
            "贴",
            "膏",
            "丸",
            "丹",
            "水",
            "液",
            "油",
            "器",
            "棒",
            "霜",
        },
        key=len,
        reverse=True,
    )
)


def normalize_entity(value: Any) -> str:
    return re.sub(r"[\s_（）()\-—·./\\]+", "", str(value or "").replace("\u00a0", " ").strip()).lower()


def alias_family(value: Any) -> str:
    text = re.sub(r"关键词", "", str(value or "").strip(), flags=re.I)
    while True:
        stripped = _VARIANT_SUFFIX.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    return normalize_entity(text)


def _split_form(value: str) -> tuple[str, str]:
    normalized = alias_family(value)
    for form in _FORM_TOKENS:
        if normalized.endswith(form) and len(normalized) > len(form):
            return normalized[: -len(form)], form
    return normalized, ""


def _tokens(value: Any) -> list[str]:
    return [normalize_entity(item) for item in re.split(r"[\s_（）()\-—·./\\]+", str(value or "")) if normalize_entity(item)]


def _longest_common_substring(left: str, right: str) -> str:
    if not left or not right:
        return ""
    previous = [0] * (len(right) + 1)
    best_length = 0
    best_end = 0
    for i, lchar in enumerate(left, 1):
        current = [0] * (len(right) + 1)
        for j, rchar in enumerate(right, 1):
            if lchar == rchar:
                current[j] = previous[j - 1] + 1
                if current[j] > best_length:
                    best_length = current[j]
                    best_end = i
        previous = current
    return left[best_end - best_length : best_end]


def _evidence(kind: str, **details: Any) -> dict[str, Any]:
    return {"type": kind, **details}


def semantic_evidence(source: str, candidate: str) -> list[dict[str, Any]]:
    source_name = alias_family(source)
    candidate_name = alias_family(candidate)
    source_root, source_form = _split_form(source)
    candidate_root, candidate_form = _split_form(candidate)
    evidence: list[dict[str, Any]] = []

    if source_name and source_name == candidate_name:
        evidence.append(_evidence("normalized_exact_name"))
    if source_root and source_root == candidate_root and source_name != candidate_name:
        evidence.append(_evidence("product_form_root_exact", root=source_root))
    shorter = min(len(source_root), len(candidate_root))
    if shorter >= 2 and (source_root in candidate_root or candidate_root in source_root) and source_root != candidate_root:
        evidence.append(_evidence("normalized_containment", source_root=source_root, candidate_root=candidate_root))
    if source_root and len(source_root) == 1 and source_root in candidate_root:
        evidence.append(_evidence("short_semantic_stem", stem=source_root))
    if source_root and candidate_root and len(source_root) == len(candidate_root):
        differences = sum(left != right for left, right in zip(source_root, candidate_root))
        if differences == 1:
            evidence.append(_evidence("single_character_root_variant", source_root=source_root, candidate_root=candidate_root))
        if len(source_root) >= 2 and source_root != candidate_root and sorted(source_root) == sorted(candidate_root):
            evidence.append(_evidence("transposed_root_variant", source_root=source_root, candidate_root=candidate_root))
    anchor = _longest_common_substring(source_root, candidate_root)
    if len(anchor) >= 2 and source_root != candidate_root:
        evidence.append(_evidence("shared_semantic_anchor", anchor=anchor))
    elif len(anchor) == 1 and min(len(source_root), len(candidate_root)) <= 2:
        evidence.append(_evidence("weak_shared_anchor", anchor=anchor))
    if source_form and candidate_form and source_form == candidate_form:
        evidence.append(_evidence("same_product_form", form=source_form))
    shared_tokens = sorted(
        {
            token
            for token in _tokens(source)
            if len(token) >= 2 and any(
                token == other or token in other or other in token
                for other in _tokens(candidate)
                if len(other) >= 2
            )
        },
        key=lambda item: (-len(item), item),
    )
    if shared_tokens:
        evidence.append(_evidence("shared_distinct_token", tokens=shared_tokens))
    return evidence


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        key = normalize_entity(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _dedupe_evidence(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in items:
        marker = repr(sorted(item.items()))
        if marker not in seen:
            seen.add(marker)
            result.append(item)
    return result


def evidence_classes(items: Iterable[dict[str, Any]]) -> list[str]:
    types = {str(item.get("type") or "") for item in items}
    classes = []
    for name, members in (
        (HARD_IDENTITY, _HARD_IDENTITY_TYPES),
        (DETERMINISTIC_STRUCTURE, _DETERMINISTIC_STRUCTURE_TYPES),
        (SEMANTIC_EVIDENCE, _SEMANTIC_TYPES),
        (CROSS_FILE_EVIDENCE, _CROSS_FILE_TYPES),
        (GLOBAL_RECONCILIATION, _GLOBAL_RECONCILIATION_TYPES),
        (EXCLUSIVITY_EVIDENCE, _EXCLUSIVITY_TYPES),
    ):
        if types & members:
            classes.append(name)
    return classes


def finalize_resolution(decision: dict[str, Any]) -> dict[str, Any]:
    """Attach an evidence-based class and review policy without inventing a score."""
    item = dict(decision)
    evidence = _dedupe_evidence(item.get("evidence", []))
    contradictions = _dedupe_evidence(item.get("contradictions", []))
    decision_type = str(item.get("decision") or HUMAN_REQUIRED)
    if decision_type == MACHINE_INFERRED:
        decision_type = INFERRED_REVIEW
    if contradictions:
        decision_type = HUMAN_REQUIRED
    classes = evidence_classes(evidence)
    evidence_types = {str(entry.get("type") or "") for entry in evidence}
    confirmation_provenance = "HUMAN_CONFIRMED" if evidence_types & {
        "human_confirmed_local_mapping", "confirmed_run_mapping", "human_confirmed_store_assignment", "human_confirmed_review"
    } else None
    if decision_type == VERIFIED:
        risk = None
        reason = "strong_identity_or_controlled_deterministic_relation"
    elif decision_type == INFERRED_REVIEW:
        strong_combination = (
            GLOBAL_RECONCILIATION in classes
            and (CROSS_FILE_EVIDENCE in classes or EXCLUSIVITY_EVIDENCE in classes)
        ) or (
            SEMANTIC_EVIDENCE in classes
            and EXCLUSIVITY_EVIDENCE in classes
            and (DETERMINISTIC_STRUCTURE in classes or CROSS_FILE_EVIDENCE in classes or GLOBAL_RECONCILIATION in classes)
        )
        risk = "LOW_REVIEW_RISK" if strong_combination else "MEDIUM_REVIEW_RISK"
        reason = "unique_evidence_backed_inference_requires_human_review"
    else:
        risk = "HIGH_REVIEW_RISK"
        reason = "no_unique_contradiction_free_answer"
    item.update(
        decision=decision_type,
        decision_type=decision_type,
        candidate=item.get("candidate"),
        selected_answer=item.get("candidate"),
        evidence=evidence,
        evidence_classes=classes,
        supporting_evidence=evidence,
        contradictions=contradictions,
        contradictions_checked={"status": "FAIL" if contradictions else "PASS", "items": contradictions},
        alternatives=list(item.get("alternatives") or []),
        reconciliation_result=dict(item.get("reconciliation") or {"status": "NOT_TESTED"}),
        reason=reason,
        human_review_required=decision_type in {INFERRED_REVIEW, HUMAN_REQUIRED},
        review_risk=risk,
        confirmation_provenance=confirmation_provenance,
    )
    return item


def resolve_entity(
    source: str,
    candidates: Iterable[str],
    *,
    entity_type: str,
    exact_matches: Iterable[tuple[str, str, dict[str, Any] | None]] = (),
    context_candidates: Iterable[str] | None = None,
    semantic_candidates: Iterable[str] | None = None,
    source_scope: str | None = None,
    sibling_sources: Iterable[str] = (),
    cross_file_targets: Iterable[str] = (),
    reconciliation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    all_candidates = _unique(candidates)
    by_key = {normalize_entity(item): item for item in all_candidates}
    context_supplied = context_candidates is not None
    context = _unique(context_candidates or [])
    context_keys = {normalize_entity(item) for item in context}
    contradictions: list[dict[str, Any]] = []
    exact: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for target, evidence_type, details in exact_matches:
        canonical = by_key.get(normalize_entity(target))
        if canonical:
            exact[canonical].append(_evidence(evidence_type, **(details or {})))
            if context_supplied and normalize_entity(canonical) not in context_keys:
                contradictions.append(_evidence("exact_target_outside_current_context", target=canonical, evidence_type=evidence_type))
        elif target:
            contradictions.append(_evidence("exact_target_outside_template", target=target, evidence_type=evidence_type))

    base = {
        "schema_version": 1,
        "entity_type": entity_type,
        "source": source,
        "sources": [source],
        "fact_family": f"{entity_type}:{alias_family(source)}",
        "candidate": None,
        "decision": HUMAN_REQUIRED,
        "evidence": [],
        "contradictions": contradictions,
        "alternatives": [],
        "reconciliation": reconciliation or {"status": "NOT_TESTED"},
        "candidate_generation": [],
    }
    if len(exact) == 1 and not contradictions:
        candidate = next(iter(exact))
        exact_evidence = _dedupe_evidence([*exact[candidate], _evidence("unique_exact_target")])
        base.update(
            candidate=candidate,
            decision=VERIFIED,
            evidence=exact_evidence,
            candidate_generation=[{
                "candidate": candidate,
                "evidence": exact_evidence,
                "source_scope": "deterministic_identity",
                "reason": "unique_exact_target_without_contradiction",
            }],
        )
        return finalize_resolution(base)
    if len(exact) > 1:
        base["contradictions"].append(_evidence("conflicting_exact_targets", targets=sorted(exact)))
        base["alternatives"] = sorted(exact)
        return finalize_resolution(base)

    semantic_supplied = semantic_candidates is not None
    semantic_scope = _unique(semantic_candidates or [])
    semantic_keys = {normalize_entity(item) for item in semantic_scope}
    if context_supplied:
        search_candidates = [item for item in all_candidates if normalize_entity(item) in context_keys]
        resolved_scope = source_scope or "current_store_products"
    elif semantic_supplied:
        search_candidates = [item for item in all_candidates if normalize_entity(item) in semantic_keys]
        resolved_scope = source_scope or "explicit_bounded_scope"
    else:
        search_candidates = list(all_candidates)
        resolved_scope = source_scope or "current_template_products"
    semantic: dict[str, list[dict[str, Any]]] = {}
    for candidate in search_candidates:
        evidence = semantic_evidence(source, candidate)
        if evidence:
            semantic[candidate] = evidence

    cross_targets = _unique(by_key.get(normalize_entity(item), "") for item in cross_file_targets)
    cross_targets = [item for item in cross_targets if item]
    if len(cross_targets) > 1:
        base["contradictions"].append(_evidence("cross_file_targets_conflict", targets=sorted(cross_targets)))
    for target in cross_targets:
        if not context_supplied or normalize_entity(target) in context_keys:
            semantic.setdefault(target, []).append(_evidence("cross_file_identity_corroboration"))

    source_tokens = [token for token in _tokens(source) if len(token) >= 2]
    for token in source_tokens:
        matching = [
            candidate
            for candidate in search_candidates
            if any(
                token == other or token in other or other in token
                for other in _tokens(candidate)
                if len(other) >= 2
            )
        ]
        if len(matching) == 1 and matching[0] in semantic:
            semantic[matching[0]].append(_evidence("unique_shared_token", token=token))

    plausible = sorted(semantic)
    base["candidate_generation"] = [
        {
            "candidate": candidate,
            "evidence": _dedupe_evidence(items),
            "source_scope": resolved_scope,
            "reason": "semantic_evidence_within_bounded_source_scope",
        }
        for candidate, items in sorted(semantic.items())
    ]
    strong_candidates = []
    for candidate, items in semantic.items():
        types = {item["type"] for item in items}
        if types & {"product_form_root_exact", "normalized_containment", "unique_shared_token", "cross_file_identity_corroboration"}:
            strong_candidates.append(candidate)
        elif (
            {"single_character_root_variant", "same_product_form"} <= types
            or {"shared_semantic_anchor", "same_product_form"} <= types
            or {"transposed_root_variant", "same_product_form"} <= types
        ):
            strong_candidates.append(candidate)
    decision_candidates = sorted(strong_candidates) if strong_candidates else plausible
    base["alternatives"] = decision_candidates
    if len(decision_candidates) != 1 or base["contradictions"]:
        return finalize_resolution(base)

    candidate = decision_candidates[0]
    evidence = list(semantic[candidate])
    rejected_weak = sorted(set(plausible) - set(decision_candidates))
    if rejected_weak:
        evidence.append(_evidence("weaker_semantic_candidates_rejected", candidates=rejected_weak))
    evidence.append(_evidence("unique_candidate_in_current_template"))
    if context_supplied and normalize_entity(candidate) in context_keys:
        evidence.append(_evidence("current_store_product_scope", candidate_count=len(context_keys)))
    siblings = _unique(sibling_sources)
    if len(siblings) >= 2 and len({alias_family(item) for item in siblings}) == 1:
        evidence.append(_evidence("sibling_alias_family", sources=siblings))
    if reconciliation and reconciliation.get("status") == "PASS":
        evidence.append(_evidence("reconciliation_consistent", **{k: v for k, v in reconciliation.items() if k != "status"}))
    elif reconciliation and reconciliation.get("status") == "FAIL":
        base["contradictions"].append(_evidence("reconciliation_conflict", **{k: v for k, v in reconciliation.items() if k != "status"}))

    types = {item["type"] for item in evidence}
    strong_semantic = bool(
        types
        & {
            "product_form_root_exact",
            "normalized_containment",
            "shared_distinct_token",
            "unique_shared_token",
        }
    ) or (
        {"single_character_root_variant", "same_product_form"} <= types
        or {"shared_semantic_anchor", "same_product_form"} <= types
        or {"transposed_root_variant", "same_product_form"} <= types
    )
    contextual_support = types & {
        "current_store_product_scope",
        "sibling_alias_family",
        "cross_file_identity_corroboration",
        "reconciliation_consistent",
    }
    weak_semantic = bool(types & {"short_semantic_stem", "weak_shared_anchor", "single_character_root_variant"})
    cross_file_supported = "cross_file_identity_corroboration" in types and bool(
        types & {"current_store_product_scope", "sibling_alias_family", "reconciliation_consistent"}
    )
    if not base["contradictions"] and (strong_semantic or cross_file_supported or (weak_semantic and len(contextual_support) >= 2)):
        base.update(candidate=candidate, decision=INFERRED_REVIEW, evidence=_dedupe_evidence(evidence), alternatives=[])
    else:
        base.update(candidate=candidate, evidence=_dedupe_evidence(evidence), alternatives=[])
    return finalize_resolution(base)


def resolve_global_store_constraints(
    file_facts: Iterable[dict[str, Any]],
    ledger_totals: dict[str, int],
    assigned_totals: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Find a unique current-run file-to-store assignment from exact cent constraints."""
    assigned = {store: int((assigned_totals or {}).get(store, 0)) for store in ledger_totals}
    remaining = {store: int(total) - assigned.get(store, 0) for store, total in ledger_totals.items()}
    facts = []
    for raw in file_facts:
        total = int(raw.get("total_cents", 0))
        candidates = _unique(str(item) for item in raw.get("candidate_stores", []) if str(item) in ledger_totals)
        if total > 0:
            facts.append({**raw, "total_cents": total, "candidate_stores": candidates})

    base = {
        "status": HUMAN_REQUIRED,
        "solutions_found": 0,
        "assignments": {},
        "decisions": {},
        "reconciliation": {"status": "NOT_TESTED", "remaining_by_store_cents": remaining},
    }
    if (
        not facts
        or any(value < 0 for value in remaining.values())
        or any(not item["candidate_stores"] for item in facts)
        or any(not item.get("products") for item in facts)
        or any(bool(item.get("has_conflict")) for item in facts)
    ):
        base["reconciliation"] = {
            "status": "FAIL",
            "reason": "missing_non_amount_constraint_or_conflicting_file_evidence",
            "remaining_by_store_cents": remaining,
        }
        return base
    if sum(item["total_cents"] for item in facts) != sum(remaining.values()):
        base["reconciliation"] = {
            "status": "FAIL",
            "file_total_cents": sum(item["total_cents"] for item in facts),
            "ledger_remaining_cents": sum(remaining.values()),
        }
        return base

    ordered = sorted(facts, key=lambda item: (len(item["candidate_stores"]), -item["total_cents"], str(item["id"])))
    solutions: list[dict[str, str]] = []
    running = {store: 0 for store in ledger_totals}

    def search(index: int, allocation: dict[str, str]) -> None:
        if len(solutions) >= 2:
            return
        if index == len(ordered):
            if all(running[store] == remaining[store] for store in remaining):
                solutions.append(dict(allocation))
            return
        fact = ordered[index]
        amount = fact["total_cents"]
        for store in sorted(fact["candidate_stores"]):
            if running[store] + amount > remaining[store]:
                continue
            running[store] += amount
            allocation[str(fact["id"])] = store
            search(index + 1, allocation)
            allocation.pop(str(fact["id"]), None)
            running[store] -= amount

    search(0, {})
    base["solutions_found"] = len(solutions)
    if len(solutions) != 1:
        base["reconciliation"] = {
            "status": "AMBIGUOUS" if solutions else "FAIL",
            "remaining_by_store_cents": remaining,
            "solutions_found": len(solutions),
        }
        return base

    allocation = solutions[0]
    decisions: dict[str, dict[str, Any]] = {}
    by_id = {str(item["id"]): item for item in facts}
    for fact_id, store in allocation.items():
        fact = by_id[fact_id]
        candidates = list(fact["candidate_stores"])
        evidence = [
            _evidence("global_constraint_unique_solution", solutions_found=1),
            _evidence("current_file_amount_cents", amount=fact["total_cents"]),
            _evidence("candidate_store_scope", stores=candidates, products=sorted(fact.get("products", []))),
            _evidence(
                "ledger_remainder_exact_across_current_files",
                store=store,
                ledger_total_cents=ledger_totals[store],
                previously_assigned_cents=assigned.get(store, 0),
            ),
            _evidence("current_run_evidence_only", filename_history_used=False),
        ]
        decisions[fact_id] = {
            "schema_version": 1,
            "entity_type": "store",
            "source": str(fact.get("source") or fact_id),
            "sources": [str(fact.get("source") or fact_id)],
            "fact_family": f"store-file:{fact_id}",
            "candidate": store,
            "decision": INFERRED_REVIEW,
            "evidence": evidence,
            "contradictions": [],
            "alternatives": [],
            "candidate_generation": [{
                "candidate": candidate,
                "evidence": [_evidence("candidate_store_scope")],
                "source_scope": "current_template_store_product_structure",
                "reason": "current_file_products_are_contained_by_store",
            } for candidate in candidates],
            "reconciliation": {
                "status": "PASS",
                "method": "unique_global_cent_constraint_solution",
                "ledger_totals_cents": ledger_totals,
                "assigned_totals_cents": assigned,
                "current_run_assignments": allocation,
            },
        }
        decisions[fact_id] = finalize_resolution(decisions[fact_id])
    base.update(
        status=INFERRED_REVIEW,
        assignments=allocation,
        decisions=decisions,
        reconciliation={"status": "PASS", "remaining_by_store_cents": remaining},
    )
    return base


def merge_human_decisions(decisions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        if decision.get("decision") != HUMAN_REQUIRED:
            continue
        grouped[(str(decision.get("entity_type")), str(decision.get("fact_family")))].append(decision)

    merged = []
    for (_, _), items in grouped.items():
        first = dict(items[0])
        sources = _unique(source for item in items for source in item.get("sources", [item.get("source", "")]))
        candidates = _unique(str(item.get("candidate") or "") for item in items)
        alternatives = _unique(str(value) for item in items for value in item.get("alternatives", []))
        first["source"] = sources[0] if len(sources) == 1 else " / ".join(sources)
        first["sources"] = sources
        first["candidate"] = candidates[0] if len(candidates) == 1 else None
        first["alternatives"] = alternatives
        first["evidence"] = _dedupe_evidence(item for decision in items for item in decision.get("evidence", []))
        first["contradictions"] = _dedupe_evidence(item for decision in items for item in decision.get("contradictions", []))
        first["gate_types"] = sorted({str(item.get("gate_type")) for item in items if item.get("gate_type")})
        first["contexts"] = [context for item in items for context in item.get("contexts", [])]
        first["mapping_sources"] = _unique(source for item in items for source in item.get("mapping_sources", []))
        target_kinds = {str(item.get("target_kind")) for item in items if item.get("target_kind")}
        if len(target_kinds) == 1:
            first["target_kind"] = next(iter(target_kinds))
        merged.append(first)
    return merged


def merge_inferred_decisions(decisions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Coalesce records into independent business decisions for one review batch."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw in decisions:
        decision = finalize_resolution(raw)
        if decision.get("decision") != INFERRED_REVIEW:
            continue
        evidence_types = {str(item.get("type") or "") for item in decision.get("evidence", [])}
        if "global_constraint_unique_solution" in evidence_types:
            family = "global-allocation"
        else:
            family = str(decision.get("fact_family") or alias_family(decision.get("source")))
        grouped[(str(decision.get("entity_type")), family, str(decision.get("candidate") or ""))].append(decision)

    merged = []
    for items in grouped.values():
        first = dict(items[0])
        sources = _unique(source for item in items for source in item.get("sources", [item.get("source", "")]))
        first["source"] = sources[0] if len(sources) == 1 else " / ".join(sources)
        first["sources"] = sources
        first["evidence"] = _dedupe_evidence(evidence for item in items for evidence in item.get("evidence", []))
        first["contradictions"] = _dedupe_evidence(
            contradiction for item in items for contradiction in item.get("contradictions", [])
        )
        first["contexts"] = [context for item in items for context in item.get("contexts", [])]
        first["mapping_sources"] = _unique(source for item in items for source in item.get("mapping_sources", []))
        first["member_decisions"] = [
            {
                "entity_type": item.get("entity_type"),
                "sources": list(item.get("sources") or [item.get("source")]),
                "candidate": item.get("candidate"),
            }
            for item in items
        ]
        target_kinds = {str(item.get("target_kind")) for item in items if item.get("target_kind")}
        if len(target_kinds) == 1:
            first["target_kind"] = next(iter(target_kinds))
        merged.append(finalize_resolution(first))
    return merged
