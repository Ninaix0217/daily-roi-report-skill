from __future__ import annotations

import re
from typing import Any


class ReviewReplyError(ValueError):
    pass


_CORRECTION_PATTERNS = (
    re.compile(r"(?P<number>\d+)\s*(?:改为|改成|改成了)\s*(?P<target>[^，,；;。]+)"),
    re.compile(r"(?P<number>\d+)\s*不对\s*[，,、]?\s*(?:是|应为)\s*(?P<target>[^，,；;。]+)"),
)


def persistence_eligibility(item: dict[str, Any]) -> dict[str, Any]:
    """Classify review persistence without exposing internal scope controls."""
    resolution = dict(item.get("resolution_candidate") or {})
    sources = [str(value) for value in resolution.get("sources", []) if str(value or "").strip()]
    entity_type = str(resolution.get("entity_type") or "")
    target = str(item.get("proposed_answer") or resolution.get("target") or "").strip()
    evidence_types = {str(value.get("type") or "") for value in item.get("supporting_evidence", [])}
    run_only_tokens = {"full_store_export"}
    run_only = (
        bool(item.get("run_only"))
        or "global_constraint_unique_solution" in evidence_types
        or any(source in run_only_tokens or source.startswith("regular_store:") for source in sources)
        or (entity_type == "campaign" and resolution.get("target_kind") == "store")
    )
    if run_only:
        return {"eligible": False, "reason": "current_run_allocation", "memory_type": None, "scope": None}
    if not entity_type or not sources or not target:
        return {"eligible": False, "reason": "missing_stable_identity", "memory_type": None, "scope": None}
    memory_type = {"campaign": "PLAN_PATTERN", "store": "STORE_MAPPING"}.get(entity_type, "ENTITY_MAPPING")
    scope: dict[str, Any] = {}
    contexts = list(item.get("contexts") or [])
    stores = {str(context.get("store") or context.get("template_store") or "").strip() for context in contexts}
    stores.discard("")
    if len(stores) == 1 and memory_type == "PLAN_PATTERN":
        scope["store"] = next(iter(stores))
    return {
        "eligible": True,
        "reason": "stable_scoped_business_mapping",
        "memory_type": memory_type,
        "scope": scope,
        "entity_type": entity_type,
        "sources": sources,
        "target": target,
    }


def render_review_batch(
    batch: dict[str, Any] | None,
    *,
    verified_records: int = 0,
    human_gates: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    gates = list(human_gates or [])
    if (not batch or not batch.get("items")) and not gates:
        return None
    items = list((batch or {}).get("items", []))
    focus = [item for item in items if item.get("review_risk") in {"MEDIUM_REVIEW_RISK", "HIGH_REVIEW_RISK"} or item.get("alternatives")]
    low = [item for item in items if item not in focus]
    lines = [f"自动确认：{verified_records} 项", "", "AI 已完成以下判断，请快速审阅："]

    def section(title: str, section_items: list[dict[str, Any]]) -> None:
        if not section_items:
            return
        lines.extend(["", f"### {title}", ""])
        for item in section_items:
            sources = " / ".join(str(value) for value in item.get("sources", []))
            lines.append(f"{item['number']}. {sources}")
            lines.append(f"   → 建议：{item['proposed_answer']}")
            alternatives = [str(value) for value in item.get("alternatives", []) if str(value)]
            if alternatives:
                lines.append(f"   备选：{' / '.join(alternatives)}")
            lines.append("")
            lines.append("   主要依据：")
            for reason in item.get("evidence_summary", []):
                lines.append(f"   - {reason}")
            lines.append("")

    section("建议重点确认", focus)
    section("低风险建议", low)

    gate_replies = []
    if gates:
        lines.extend(["", "### 需要你决定", ""])
        next_number = max((int(item.get("number", 0)) for item in items), default=0) + 1
        for offset, gate in enumerate(gates):
            number = next_number + offset
            resolution = dict((gate.get("evidence") or {}).get("resolution") or {})
            hint = dict(resolution.get("useful_hint") or {})
            sources = list(resolution.get("sources") or [resolution.get("source")])
            sources = [str(value) for value in sources if str(value or "").strip()]
            lines.append(f"{number}. {' / '.join(sources) or gate.get('gate_id')}")
            if hint.get("evidence_status") == "AMOUNT_ONLY_HINT" and hint.get("candidate"):
                candidate = str(hint["candidate"])
                lines.extend([
                    "   当前没有足够身份关系自动确认。",
                    f"   金额上唯一完全匹配：{candidate}。",
                    "",
                    f"   如果正确，回复：{number}是",
                    f"   如果不正确：{number}改为正确店铺",
                    "",
                ])
                gate_replies.append(f"{number}是")
            else:
                lines.extend([f"   {gate.get('question')}", ""])
    recommended = "全部接受，符合长期记忆条件的映射记住"
    if gate_replies:
        recommended += "；" + "；".join(gate_replies)
    lines.extend(["# Recommended Reply", "", recommended])
    if focus:
        first = focus[0]
        alternative = next(iter(first.get("alternatives") or []), "正确答案")
        lines.extend(["", f"如果第{first['number']}项不对，可回复：", "", f"全部接受，但{first['number']}改为{alternative}"])
    prefilled = sum(bool(str(item.get("proposed_answer") or "").strip()) for item in items)
    return {
        "text": "\n".join(lines).rstrip(),
        "recommended_reply": recommended,
        "prefilled_review_decisions": prefilled,
        "review_decisions": len(items),
        "prefilled_review_rate": prefilled / len(items) if items else 0,
        "open_ended_human_decisions": len(gates),
    }


def parse_review_reply(
    text: str,
    batch: dict[str, Any],
    *,
    human_gates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reply = str(text or "").strip()
    if not reply:
        raise ReviewReplyError("Review reply is empty")
    by_number = {int(item["number"]): item for item in batch.get("items", [])}
    if not by_number:
        raise ReviewReplyError("Review batch has no items")
    remember = "记" in reply
    persistence = "ELIGIBLE_ONLY" if remember else "RUN_ONLY"
    corrections: dict[int, str] = {}
    for pattern in _CORRECTION_PATTERNS:
        for match in pattern.finditer(reply):
            number = int(match.group("number"))
            target = match.group("target").strip()
            target = re.sub(r"^(?:为|成|是)", "", target).strip()
            corrections[number] = target
    accept_all = bool(re.search(r"全部接受|都对|其余接受", reply))
    accepted: set[int] = set()
    for match in re.finditer(r"(?P<numbers>\d+(?:\s*[、,，]\s*\d+)*)\s*对", reply):
        accepted.update(int(value) for value in re.findall(r"\d+", match.group("numbers")))
    first_human_number = max(by_number, default=0) + 1
    human_by_number = {
        first_human_number + offset: gate
        for offset, gate in enumerate(human_gates or [])
    }
    hinted_confirmations = {
        int(match.group("number"))
        for match in re.finditer(r"(?P<number>\d+)\s*是(?:[，,；;。\s]|$)", reply)
    }
    known_numbers = set(by_number) | set(human_by_number)
    unknown = sorted((set(corrections) | accepted | hinted_confirmations) - known_numbers)
    if unknown:
        raise ReviewReplyError(f"Unknown review numbers: {unknown}")
    responses = []
    for number, item in by_number.items():
        if number in corrections:
            responses.append({"review_id": item["review_id"], "action": "CORRECT", "target": corrections[number], "persistence": persistence})
        elif accept_all or number in accepted:
            responses.append({"review_id": item["review_id"], "action": "ACCEPT", "persistence": persistence})
    if len(responses) != len(by_number):
        missing = sorted(set(by_number) - {number for number in by_number if accept_all or number in accepted or number in corrections})
        raise ReviewReplyError(f"Review batch must be resolved together; missing numbers={missing}")
    human_responses = []
    for number, gate in human_by_number.items():
        if number in corrections:
            human_responses.append({
                "gate_id": gate["gate_id"],
                "action": "CORRECT",
                "target": corrections[number],
                "classification": "RUN_ONLY",
            })
        elif number in hinted_confirmations:
            resolution = dict((gate.get("evidence") or {}).get("resolution") or {})
            hint = dict(resolution.get("useful_hint") or {})
            if hint.get("evidence_status") != "AMOUNT_ONLY_HINT" or not hint.get("candidate"):
                raise ReviewReplyError(f"Human decision {number} has no confirmable hint")
            human_responses.append({
                "gate_id": gate["gate_id"],
                "action": "CONFIRM_HINT",
                "target": str(hint["candidate"]),
                "classification": "RUN_ONLY",
                "evidence_status": "AMOUNT_ONLY_HINT",
            })
    return {
        "responses": responses,
        "human_responses": human_responses,
        "remember_eligible": remember,
        "batch_id": batch.get("batch_id"),
    }
