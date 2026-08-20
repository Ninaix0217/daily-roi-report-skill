from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable


VERIFIED = "VERIFIED"
MACHINE_INFERRED = "MACHINE_INFERRED"
HUMAN_REQUIRED = "HUMAN_REQUIRED"

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


def resolve_entity(
    source: str,
    candidates: Iterable[str],
    *,
    entity_type: str,
    exact_matches: Iterable[tuple[str, str, dict[str, Any] | None]] = (),
    context_candidates: Iterable[str] | None = None,
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
    }
    if len(exact) == 1 and not contradictions:
        candidate = next(iter(exact))
        base.update(
            candidate=candidate,
            decision=VERIFIED,
            evidence=_dedupe_evidence([*exact[candidate], _evidence("unique_exact_target")]),
        )
        return base
    if len(exact) > 1:
        base["contradictions"].append(_evidence("conflicting_exact_targets", targets=sorted(exact)))
        base["alternatives"] = sorted(exact)
        return base

    search_candidates = [item for item in all_candidates if not context_supplied or normalize_entity(item) in context_keys]
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
    strong_candidates = []
    for candidate, items in semantic.items():
        types = {item["type"] for item in items}
        if types & {"product_form_root_exact", "normalized_containment", "unique_shared_token", "cross_file_identity_corroboration"}:
            strong_candidates.append(candidate)
        elif {"single_character_root_variant", "same_product_form"} <= types or {"shared_semantic_anchor", "same_product_form"} <= types:
            strong_candidates.append(candidate)
    decision_candidates = sorted(strong_candidates) if strong_candidates else plausible
    base["alternatives"] = decision_candidates
    if len(decision_candidates) != 1 or base["contradictions"]:
        return base

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
    ) or ({"single_character_root_variant", "same_product_form"} <= types) or ({"shared_semantic_anchor", "same_product_form"} <= types)
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
        base.update(candidate=candidate, decision=MACHINE_INFERRED, evidence=_dedupe_evidence(evidence), alternatives=[])
    else:
        base.update(candidate=candidate, evidence=_dedupe_evidence(evidence), alternatives=[])
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
