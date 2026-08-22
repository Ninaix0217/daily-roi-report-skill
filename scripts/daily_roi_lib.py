from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import warnings
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from evidence_resolution import (
    HUMAN_REQUIRED,
    INFERRED_REVIEW,
    VERIFIED,
    alias_family,
    finalize_resolution,
    merge_human_decisions,
    merge_inferred_decisions,
    resolve_entity,
    resolve_global_store_constraints,
    union_cross_path_evidence,
)
from review_ux import parse_review_reply, persistence_eligibility, render_review_batch


MONEY = Decimal("0.01")
STATE_DIR_NAME = ".daily-roi"
MEMORY_FILE = "memory.json"
CONFIRMATIONS_FILE = "confirmations.jsonl"
CURRENT_RUN_FILE = "current-run.json"
SUPPORTED_MAPPING_TYPES = {"store", "product", "campaign", "sku"}
WORKFLOW_RULE_STALE_TEMPLATE = "auto_update_stale_template_date"
MEMORY_TYPES = {"ENTITY_MAPPING", "PLAN_PATTERN", "STORE_MAPPING", "WORKFLOW_PREFERENCE"}
MEMORY_STATUSES = {"ACTIVE", "SUPERSEDED", "CONFLICTED", "RETIRED"}
SOURCE_DATE_RE = re.compile(r"(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)")


class DailyRoiError(RuntimeError):
    pass


class HumanRequired(DailyRoiError):
    def __init__(self, gates: list[dict[str, Any]]):
        super().__init__(f"{len(gates)} human gate(s) require resolution")
        self.gates = gates


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def norm(value: Any) -> str:
    return re.sub(r"[\s_（）()\-—]+", "", str(value or "").replace("\u00a0", " ").strip()).lower()


def decimal_money(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0.00")
    try:
        return Decimal(str(value).replace(",", "").strip()).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, AttributeError) as exc:
        raise DailyRoiError(f"Invalid monetary value: {value!r}") from exc


def to_cents(value: Any) -> int:
    return int((decimal_money(value) * 100).to_integral_exact())


def cents_text(value: int) -> str:
    return f"{Decimal(value) / 100:.2f}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


@dataclass(frozen=True)
class RuntimePaths:
    workspace: Path
    state_dir: Path
    memory: Path
    confirmations: Path
    current_run: Path
    runs_dir: Path

    @classmethod
    def for_workspace(cls, workspace: Path) -> "RuntimePaths":
        root = workspace.resolve()
        state = root / STATE_DIR_NAME
        return cls(root, state, state / MEMORY_FILE, state / CONFIRMATIONS_FILE, state / CURRENT_RUN_FILE, state / "runs")


class LocalMemory:
    def __init__(self, paths: RuntimePaths):
        self.paths = paths
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.paths.memory.exists():
            return {"schema_version": 2, "entity_mappings": [], "workflow_rules": [], "rejected_proposals": []}
        data = json.loads(self.paths.memory.read_text(encoding="utf-8"))
        self.validate(data)
        if data.get("schema_version") == 1:
            data = self._migrate_v1(data)
        return data

    def _migrate_v1(self, data: dict[str, Any]) -> dict[str, Any]:
        migrated = {"schema_version": 2, "entity_mappings": [], "workflow_rules": list(data.get("workflow_rules", [])), "rejected_proposals": []}
        for item in data.get("entity_mappings", []):
            entity_type = str(item["entity_type"])
            migrated["entity_mappings"].append({
                **item,
                "memory_id": item.get("memory_id") or "MEM-" + hashlib.sha256(
                    json.dumps([entity_type, item["source"], item["target"], item.get("gate_id")], ensure_ascii=False).encode("utf-8")
                ).hexdigest()[:12],
                "memory_type": self.memory_type_for(entity_type),
                "status": "ACTIVE",
                "scope": {"workspace": str(self.paths.workspace)},
                "confirmation_mode": "HUMAN_CORRECTION",
                "created_at": item.get("confirmed_at") or now_iso(),
                "last_used": None,
                "use_count": 0,
                "original_proposal": None,
                "evidence_at_confirmation": [],
                "source_run": None,
                "decision_id": item.get("gate_id"),
                "lineage_id": item.get("gate_id") or None,
            })
        return migrated

    @staticmethod
    def validate(data: dict[str, Any]) -> None:
        version = data.get("schema_version")
        if version not in {1, 2}:
            raise DailyRoiError("Unsupported local memory schema_version")
        if not isinstance(data.get("entity_mappings"), list) or not isinstance(data.get("workflow_rules"), list):
            raise DailyRoiError("Invalid local memory collections")
        if version == 2 and not isinstance(data.get("rejected_proposals"), list):
            raise DailyRoiError("Invalid rejected proposal audit collection")
        for item in data["entity_mappings"]:
            if item.get("kind") != "entity_mapping" or item.get("entity_type") not in SUPPORTED_MAPPING_TYPES:
                raise DailyRoiError(f"Invalid entity mapping: {item}")
            allowed_status = {"confirmed"} if version == 1 else MEMORY_STATUSES
            if item.get("status") not in allowed_status or item.get("source_type") != "human_confirmation":
                raise DailyRoiError("Only human-confirmed mappings may be durable")
            if not str(item.get("source", "")).strip() or not str(item.get("target", "")).strip():
                raise DailyRoiError("Mapping source and target are required")
            if version == 2:
                if item.get("memory_type") not in MEMORY_TYPES or not isinstance(item.get("scope"), dict):
                    raise DailyRoiError("Memory v2 mappings require a type and explicit scope")
                if item.get("confirmation_mode") not in {"REVIEW_ACCEPT", "HUMAN_CORRECTION", "HUMAN_GATE_CONFIRM"}:
                    raise DailyRoiError("Invalid memory confirmation mode")
        for item in data["workflow_rules"]:
            if item.get("kind") != "workflow_rule" or item.get("status") != "confirmed":
                raise DailyRoiError(f"Invalid workflow rule: {item}")
            if item.get("rule") != WORKFLOW_RULE_STALE_TEMPLATE:
                raise DailyRoiError(f"Unsupported workflow rule: {item.get('rule')}")

    def save(self) -> None:
        self.validate(self.data)
        atomic_json(self.paths.memory, self.data)

    @staticmethod
    def memory_type_for(entity_type: str) -> str:
        return {"campaign": "PLAN_PATTERN", "store": "STORE_MAPPING"}.get(entity_type, "ENTITY_MAPPING")

    @staticmethod
    def _scope_applies(stored: dict[str, Any], current: dict[str, Any] | None) -> bool:
        current = current or {}
        for key, value in stored.items():
            if key == "workspace" and key not in current:
                continue
            if key not in current or norm(current[key]) != norm(value):
                return False
        return True

    def find(self, entity_type: str, source: str, *, scope: dict[str, Any] | None = None) -> dict[str, Any] | None:
        wanted = norm(source)
        for item in reversed(self.data["entity_mappings"]):
            active = item.get("status") in {"confirmed", "ACTIVE"}
            if active and item["entity_type"] == entity_type and norm(item["source"]) == wanted and self._scope_applies(item.get("scope", {}), scope):
                return item
        return None

    def resolve(
        self,
        entity_type: str,
        source: str,
        *,
        scope: dict[str, Any] | None = None,
        valid_targets: Iterable[str] | None = None,
        hard_identity_target: str | None = None,
    ) -> str | None:
        item = self.find(entity_type, source, scope=scope)
        if not item:
            return None
        target = str(item["target"])
        if valid_targets is not None and norm(target) not in {norm(value) for value in valid_targets}:
            item["status"] = "RETIRED"
            item["retired_at"] = now_iso()
            item["retired_reason"] = "target_absent_from_current_template"
            self.save()
            return None
        if hard_identity_target and norm(hard_identity_target) != norm(target):
            self.mark_conflicted(str(item.get("memory_id") or ""), current_hard_target=hard_identity_target)
            return None
        return target

    def has_rule(self, rule: str) -> bool:
        return any(item.get("rule") == rule and item.get("status") == "confirmed" for item in self.data["workflow_rules"])

    def add_mapping(
        self,
        entity_type: str,
        source: str,
        target: str,
        *,
        gate_id: str,
        memory_type: str | None = None,
        scope: dict[str, Any] | None = None,
        confirmation_mode: str = "HUMAN_GATE_CONFIRM",
        original_proposal: str | None = None,
        evidence_at_confirmation: Iterable[Any] = (),
        source_run: str | None = None,
        supersede: bool = False,
    ) -> dict[str, Any]:
        if entity_type not in SUPPORTED_MAPPING_TYPES:
            raise DailyRoiError(f"Unsupported mapping entity_type: {entity_type}")
        memory_type = memory_type or self.memory_type_for(entity_type)
        if memory_type not in MEMORY_TYPES:
            raise DailyRoiError(f"Unsupported memory_type: {memory_type}")
        memory_scope = {"workspace": str(self.paths.workspace), **(scope or {})}
        current_item = self.find(entity_type, source, scope=memory_scope)
        previous_item = next((
            item for item in reversed(self.data["entity_mappings"])
            if item.get("entity_type") == entity_type
            and norm(item.get("source")) == norm(source)
            and self._scope_applies(item.get("scope", {}), memory_scope)
        ), None)
        if current_item and norm(current_item["target"]) != norm(target):
            if not supersede:
                raise DailyRoiError(f"Conflicting confirmed mapping for {source!r}: {current_item['target']!r} vs {target!r}")
            current_item["status"] = "SUPERSEDED"
            current_item["superseded_at"] = now_iso()
        elif current_item:
            return current_item
        created_at = now_iso()
        memory_id = "MEM-" + hashlib.sha256(json.dumps(
            [entity_type, norm(source), norm(target), memory_scope, gate_id, created_at], ensure_ascii=False, sort_keys=True
        ).encode("utf-8")).hexdigest()[:12]
        item = {
            "kind": "entity_mapping",
            "memory_id": memory_id,
            "memory_type": memory_type,
            "entity_type": entity_type,
            "source": source,
            "target": target,
            "status": "ACTIVE",
            "source_type": "human_confirmation",
            "scope": memory_scope,
            "confirmation_mode": confirmation_mode,
            "created_at": created_at,
            "confirmed_at": created_at,
            "last_used": None,
            "use_count": 0,
            "original_proposal": original_proposal,
            "proposal_rejected": bool(original_proposal and norm(original_proposal) != norm(target)),
            "evidence_at_confirmation": list(evidence_at_confirmation),
            "source_run": source_run,
            "decision_id": gate_id,
            "lineage_id": gate_id,
            "gate_id": gate_id,
        }
        predecessor = current_item if current_item and current_item.get("status") == "SUPERSEDED" else (previous_item if supersede else None)
        if predecessor and predecessor.get("memory_id") != memory_id:
            item["supersedes"] = predecessor.get("memory_id")
            predecessor["superseded_by"] = memory_id
        self.data["entity_mappings"].append(item)
        self.save()
        return item

    def record_rejected_proposal(self, *, decision_id: str, sources: Iterable[str], proposal: str, source_run: str | None = None) -> None:
        self.data.setdefault("rejected_proposals", []).append({
            "kind": "rejected_proposal",
            "decision_id": decision_id,
            "sources": list(sources),
            "proposal": proposal,
            "status": "REJECTED",
            "created_at": now_iso(),
            "source_run": source_run,
            "creates_business_fact": False,
        })
        self.save()

    def mark_conflicted(self, memory_id: str, *, current_hard_target: str) -> None:
        for item in self.data["entity_mappings"]:
            if item.get("memory_id") == memory_id and item.get("status") == "ACTIVE":
                item["status"] = "CONFLICTED"
                item["conflicted_at"] = now_iso()
                item["conflict_target"] = current_hard_target
                self.save()
                return

    def add_rule(self, rule: str, *, gate_id: str) -> None:
        if rule != WORKFLOW_RULE_STALE_TEMPLATE:
            raise DailyRoiError(f"Unsupported workflow rule: {rule}")
        if self.has_rule(rule):
            return
        self.data["workflow_rules"].append({
            "kind": "workflow_rule",
            "rule": rule,
            "condition": "all_business_sources_same_date_and_template_only_differs",
            "action": "update_template_date",
            "status": "confirmed",
            "source_type": "human_confirmation",
            "confirmed_at": now_iso(),
            "gate_id": gate_id,
        })
        self.save()


def find_node_modules(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    for value in (explicit, os.environ.get("DAILY_ROI_NODE_MODULES"), os.environ.get("CODEX_NODE_MODULES"), os.environ.get("NODE_PATH")):
        if value:
            candidates.append(Path(value))
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    candidates.extend([
        profile / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "node_modules",
        profile / ".codex" / "node_modules",
    ])
    for candidate in candidates:
        if (candidate / "@oai" / "artifact-tool").exists():
            return candidate.resolve()
    raise DailyRoiError("@oai/artifact-tool was not found. Pass --node-modules from Codex load_workspace_dependencies.")


def find_node(explicit: str | None = None) -> Path:
    candidates = [explicit, os.environ.get("DAILY_ROI_NODE"), shutil.which("node")]
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    candidates.append(str(profile / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate).resolve()
    raise DailyRoiError("Node.js runtime not found. Pass --node from Codex load_workspace_dependencies.")


class WorkbookBridge:
    def __init__(self, node: Path, node_modules: Path):
        self.node = node
        self.node_modules = node_modules
        self.worker = Path(__file__).with_name("workbook_worker.mjs")

    def call(self, command: str, *args: Path | str) -> dict[str, Any]:
        env = os.environ.copy()
        env["DAILY_ROI_NODE_MODULES"] = str(self.node_modules)
        proc = subprocess.run(
            [str(self.node), str(self.worker), command, *(str(arg) for arg in args)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        if proc.returncode:
            raise DailyRoiError(f"Workbook worker failed ({command}): {proc.stderr.strip()}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            # artifact-tool may emit a one-line inspect spill notice before the
            # worker's own JSON.  The worker result is always the final JSON line.
            for line in reversed([item.strip() for item in proc.stdout.splitlines() if item.strip()]):
                if line.startswith("{"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        continue
            raise DailyRoiError(f"Workbook worker returned invalid JSON: {proc.stdout[:500]}") from exc

    def inspect_template(self, path: Path) -> dict[str, Any]:
        return self.call("inspect-template", path)

    def inspect_xlsx(self, path: Path) -> dict[str, Any]:
        return self.call("inspect-xlsx", path)

    def write(self, template: Path, payload: Path, output: Path) -> dict[str, Any]:
        return self.call("write", template, payload, output)

    def verify(self, template: Path, output: Path, payload: Path, render_dir: Path) -> dict[str, Any]:
        return self.call("verify", template, output, payload, render_dir)


def find_soffice() -> Path:
    candidates: list[str | None] = []
    if os.name == "nt":
        candidates.extend([
            r"C:\Program Files\LibreOffice\program\soffice.com",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ])
    candidates.extend([shutil.which("soffice"), shutil.which("libreoffice")])
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate).resolve()
    raise DailyRoiError("DEPENDENCY_CHECK=FAIL\nMISSING=LibreOffice\nLegacy .xls input requires LibreOffice/soffice. Install it through the normal IT process, then rerun.")


def dependency_preflight(
    input_dir: Path,
    *,
    node: str | None = None,
    node_modules: str | None = None,
) -> dict[str, Any]:
    """Check runtime dependencies before reading or converting business data."""
    if not input_dir.exists() or not input_dir.is_dir():
        raise DailyRoiError(f"DEPENDENCY_CHECK=FAIL\nMISSING=Input directory\nDirectory not found: {input_dir}")
    input_files = [path for path in input_dir.iterdir() if path.is_file()]
    requires_legacy_xls = any(path.suffix.lower() == ".xls" for path in input_files)
    try:
        resolved_node = find_node(node)
    except DailyRoiError as exc:
        raise DailyRoiError(f"DEPENDENCY_CHECK=FAIL\nMISSING=Node.js\n{exc}") from exc
    try:
        resolved_modules = find_node_modules(node_modules)
    except DailyRoiError as exc:
        raise DailyRoiError(f"DEPENDENCY_CHECK=FAIL\nMISSING=@oai/artifact-tool\n{exc}") from exc
    soffice = find_soffice() if requires_legacy_xls else None
    return {
        "dependency_check": "PASS",
        "input_file_count": len(input_files),
        "legacy_xls_required": requires_legacy_xls,
        "node": str(resolved_node),
        "node_modules": str(resolved_modules),
        "libreoffice": str(soffice) if soffice else "NOT_REQUIRED",
    }


def convert_xls(source: Path, run_dir: Path) -> Path:
    out_dir = run_dir / "converted"
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{sha256_file(source)[:16]}.xlsx"
    if final.exists():
        return final
    # LibreOffice on Windows is sensitive to long and non-Unicode process
    # paths.  Convert inside a short ASCII system-temp directory, then copy the
    # result into the audited run directory.  The source remains immutable.
    with tempfile.TemporaryDirectory(prefix="daily-roi-xls-") as temp_name:
        temp_root = Path(temp_name)
        staged_source = temp_root / "input.xls"
        temp_out = temp_root / "out"
        profile = temp_root / "profile"
        temp_out.mkdir()
        profile.mkdir()
        shutil.copyfile(source, staged_source)
        cmd = [
            str(find_soffice()),
            "--headless",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--convert-to",
            "xlsx:Calc MS Excel 2007 XML",
            "--outdir",
            str(temp_out),
            str(staged_source),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        converted = temp_out / "input.xlsx"
        if proc.returncode or not converted.exists():
            raise DailyRoiError(f"LibreOffice failed to convert {source.name}: {(proc.stderr or proc.stdout).strip()}")
        shutil.copyfile(converted, final)
    return final


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]], str]:
    raw = path.read_bytes()
    used = None
    content = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            content = raw.decode(encoding)
            used = encoding
            break
        except UnicodeDecodeError:
            continue
    if content is None or used is None:
        raise DailyRoiError(f"Unable to decode CSV: {path.name}")
    reader = csv.DictReader(content.splitlines())
    headers = [str(item or "").strip() for item in (reader.fieldnames or [])]
    rows = [{str(k or "").strip(): str(v or "").strip() for k, v in row.items()} for row in reader]
    return headers, rows, used


def header_index(headers: Iterable[str], *candidates: str) -> str | None:
    by_key = {norm(header): header for header in headers}
    for candidate in candidates:
        if norm(candidate) in by_key:
            return by_key[norm(candidate)]
    for header in headers:
        h = norm(header)
        if any(norm(candidate) in h for candidate in candidates):
            return header
    return None


def extract_dates_from_name(name: str) -> set[str]:
    out = set()
    for year, month, day in SOURCE_DATE_RE.findall(name):
        try:
            out.add(date(int(year), int(month), int(day)).isoformat())
        except ValueError:
            pass
    return out


def extract_dates(values: Iterable[Any]) -> set[str]:
    out: set[str] = set()
    for value in values:
        text = str(value or "")
        for year, month, day in re.findall(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text):
            try:
                out.add(date(int(year), int(month), int(day)).isoformat())
            except ValueError:
                pass
    return out


def make_gate(
    gate_type: str,
    reason: str,
    evidence: dict[str, Any],
    question: str,
    *,
    candidate: dict[str, Any] | None = None,
    persistence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stable = json.dumps([gate_type, evidence, candidate], ensure_ascii=False, sort_keys=True)
    gate_id = f"{gate_type}-{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:10]}"
    return {
        "gate_id": gate_id,
        "gate_type": gate_type,
        "status": "HUMAN_REQUIRED",
        "blocking_reason": reason,
        "evidence": evidence,
        "candidate_resolution": candidate,
        "question": question,
        "persistence_candidate": persistence,
    }


def strip_campaign_spec(value: str) -> str:
    cleaned = re.sub(r"关键词", "", str(value or "").strip(), flags=re.I)
    cleaned = re.sub(r"(?:单盒|三盒|一盒|3盒|1盒|单|[13])$", "", cleaned).strip()
    return cleaned


def resolve_product(source: str, products: list[str], memory: LocalMemory, run_mappings: dict[str, str] | None = None) -> str | None:
    product_by_key = {norm(item): item for item in products}
    if norm(source) in product_by_key:
        return product_by_key[norm(source)]
    for entity_type in ("campaign", "product", "sku"):
        target = (run_mappings or {}).get(f"{entity_type}:{norm(source)}") or memory.resolve(entity_type, source)
        if target and norm(target) in product_by_key:
            return product_by_key[norm(target)]
    stripped = strip_campaign_spec(source)
    if stripped and norm(stripped) in product_by_key:
        return product_by_key[norm(stripped)]
    if stripped and stripped != source:
        target = (run_mappings or {}).get(f"product:{norm(stripped)}") or memory.resolve("product", stripped)
        if target and norm(target) in product_by_key:
            return product_by_key[norm(target)]
    return None


def resolve_store(source: str, stores: list[str], memory: LocalMemory, run_mappings: dict[str, str] | None = None) -> str | None:
    store_by_key = {norm(item): item for item in stores}
    if norm(source) in store_by_key:
        return store_by_key[norm(source)]
    target = (run_mappings or {}).get(f"store:{norm(source)}") or memory.resolve("store", source)
    if target and norm(target) in store_by_key:
        return store_by_key[norm(target)]
    return None


def identity_type_for_header(header: Any) -> str | None:
    key = norm(header)
    aliases = {
        "sku": "sku",
        "skuid": "sku",
        "商品id": "product_id",
        "商品编号": "product_id",
        "投放id": "placement_id",
        "投放编号": "placement_id",
        "投放商品id": "platform_item_id",
        "投放商品编号": "platform_item_id",
        "平台商品id": "platform_item_id",
        "平台商品编号": "platform_item_id",
        "平台稳定商品标识": "platform_item_id",
    }
    return aliases.get(key)


def record_identity_values(record: dict[str, Any]) -> dict[str, str]:
    values = {
        str(kind): str(value).strip()
        for kind, value in dict(record.get("identities") or {}).items()
        if str(value or "").strip()
    }
    legacy = str(record.get("sku") or "").strip()
    if legacy and legacy not in values.values():
        values[str(record.get("identity_type") or "sku")] = legacy
    return values


HARD_IDENTITY_BINDING_ORIGINS = {
    "template_sku_map",
    "template_identity_map",
    "template_identity_conflict",
}


def qualify_identity_binding_as_hard(binding: dict[str, Any], source_identity_type: str) -> bool:
    """Qualify the identifier-to-product binding, not merely the identifier type."""
    if not str(binding.get("product") or "").strip():
        return False
    if str(binding.get("identity_type") or "") != str(source_identity_type or ""):
        return False
    origin = str(binding.get("origin") or "")
    trust = str(binding.get("binding_trust") or ("HARD" if origin in HARD_IDENTITY_BINDING_ORIGINS else "DERIVED"))
    return trust == "HARD" and origin in HARD_IDENTITY_BINDING_ORIGINS


def build_product_identity_index(template: dict[str, Any], campaigns: Iterable[dict[str, Any]] = ()) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def register(
        value: Any,
        product: Any,
        identity_type: str,
        origin: str,
        *,
        binding_trust: str,
        lineage_id: str | None = None,
    ) -> bool:
        key = str(value or "").strip()
        target = str(product or "").strip()
        if not key or not target:
            return False
        duplicate = next((
            item for item in index[key]
            if item.get("product") == target
            and item.get("identity_type") == identity_type
            and item.get("origin") == origin
            and item.get("binding_trust") == binding_trust
        ), None)
        if duplicate is None:
            entry = {
                "product": target,
                "identity_type": identity_type,
                "origin": origin,
                "binding_trust": binding_trust,
            }
            if lineage_id:
                entry["lineage_id"] = lineage_id
            index[key].append(entry)
            return True
        return False

    for value, mapped in ((template.get("sku") or {}).get("map") or {}).items():
        register(value, mapped.get("product"), "sku", "template_sku_map", binding_trust="HARD")
    for identity_type, mapping in ((template.get("identity") or {}).get("map") or {}).items():
        for value, mapped in dict(mapping or {}).items():
            product = mapped.get("product") if isinstance(mapped, dict) else mapped
            register(value, product, str(identity_type), "template_identity_map", binding_trust="HARD")
    for conflict in ((template.get("identity") or {}).get("conflicts") or []):
        for product in conflict.get("products", []):
            register(
                conflict.get("value"),
                product,
                str(conflict.get("identity_type") or "unknown"),
                "template_identity_conflict",
                binding_trust="HARD",
            )

    products = {norm(item["name"]): item["name"] for item in template["report"]["products"]}
    rows = [
        (str(campaign.get("name") or "current-file"), record)
        for campaign in campaigns
        for record in campaign.get("records", [])
    ]
    changed = True
    while changed:
        changed = False
        for source_file, record in rows:
            identities = record_identity_values(record)
            exact_name = products.get(norm(record.get("product_name")))
            # A derived current-run binding remains useful to resolve records
            # carrying the same identifier, but it cannot recursively create
            # more identity bindings or become independent proof of itself.
            targets = {
                entry["product"]
                for identity_type, value in identities.items()
                for entry in index.get(value, [])
                if qualify_identity_binding_as_hard(entry, identity_type)
            }
            if exact_name:
                targets.add(exact_name)
            if len(targets) != 1:
                continue
            target = next(iter(targets))
            for identity_type, value in identities.items():
                lineage_id = (
                    f"current-file-binding:{source_file}:{identity_type}:"
                    f"{str(value).strip()}:{norm(target)}"
                )
                changed = register(
                    value,
                    target,
                    identity_type,
                    "current_file_exact_identity_bridge",
                    binding_trust="DERIVED",
                    lineage_id=lineage_id,
                ) or changed
    return dict(index)


def resolve_product_evidence(
    source: str,
    template: dict[str, Any],
    memory: LocalMemory,
    run_mappings: dict[str, str],
    *,
    identity_value: str = "",
    identity_values: dict[str, str] | None = None,
    identity_index: dict[str, list[dict[str, Any]]] | None = None,
    context_products: Iterable[str] | None = None,
    semantic_products: Iterable[str] | None = None,
    source_scope: str | None = None,
    sibling_sources: Iterable[str] = (),
    cross_file_targets: Iterable[str] = (),
    reconciliation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    products = [item["name"] for item in template["report"]["products"]]
    product_by_key = {norm(item): item for item in products}
    exact_matches: list[tuple[str, str, dict[str, Any] | None]] = []
    memory_items: list[dict[str, Any]] = []
    canonical = product_by_key.get(norm(source))
    if canonical:
        exact_matches.append((canonical, "exact_template_product", None))
    for entity_type in ("campaign", "product", "sku"):
        run_target = run_mappings.get(f"{entity_type}:{norm(source)}")
        if run_target:
            exact_matches.append((run_target, "confirmed_run_mapping", {"mapping_type": entity_type}))
        memory_item = memory.find(entity_type, source)
        if memory_item:
            memory_items.append(memory_item)
            exact_matches.append((str(memory_item["target"]), "human_confirmed_local_mapping", {
                "mapping_type": entity_type,
                "memory_id": memory_item.get("memory_id"),
                "lineage_id": memory_item.get("lineage_id"),
                "memory_scope": memory_item.get("scope", {}),
            }))
    stripped = strip_campaign_spec(source)
    canonical_stripped = product_by_key.get(norm(stripped)) if stripped else None
    if canonical_stripped:
        exact_matches.append((canonical_stripped, "exact_template_product_after_spec_normalization", {"normalized_source": stripped}))
    if stripped and stripped != source:
        run_target = run_mappings.get(f"product:{norm(stripped)}")
        if run_target:
            exact_matches.append((run_target, "confirmed_run_mapping", {"mapping_type": "product", "normalized_source": stripped}))
        memory_item = memory.find("product", stripped)
        if memory_item:
            memory_items.append(memory_item)
            exact_matches.append((str(memory_item["target"]), "human_confirmed_local_mapping", {
                "mapping_type": "product", "normalized_source": stripped,
                "memory_id": memory_item.get("memory_id"), "lineage_id": memory_item.get("lineage_id"),
            }))

    sku_model = template.get("sku") or {}
    sku_map = sku_model.get("map", {})
    identities = dict(identity_values or {})
    if identity_value and str(identity_value).strip() not in identities.values():
        identities["sku"] = str(identity_value).strip()
    product_identities = identity_index if identity_index is not None else build_product_identity_index(template)
    derived_identity_matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source_identity_type, value in identities.items():
        for mapped in product_identities.get(str(value).strip(), []):
            if str(mapped.get("identity_type")) != str(source_identity_type):
                continue
            binding_details = {
                "identity_type": source_identity_type,
                "identity_value": str(value).strip(),
                "registered_identity_type": mapped["identity_type"],
                "origin": mapped["origin"],
                "binding_origin": mapped["origin"],
                "binding_trust": mapped.get("binding_trust") or (
                    "HARD" if mapped.get("origin") in HARD_IDENTITY_BINDING_ORIGINS else "DERIVED"
                ),
                "lineage_id": mapped.get("lineage_id"),
                "independent_hard_binding": qualify_identity_binding_as_hard(mapped, source_identity_type),
            }
            if not qualify_identity_binding_as_hard(mapped, source_identity_type):
                derived_identity_matches[str(mapped["product"])].append({
                    "type": "current_run_derived_identity_binding",
                    **binding_details,
                })
                continue
            evidence_type = "exact_template_sku" if mapped["identity_type"] == "sku" else "exact_template_product_identity"
            exact_matches.append((mapped["product"], evidence_type, binding_details))
        for conflict in sku_model.get("conflicts", []):
            if str(conflict.get("sku")) == str(value).strip():
                for target in conflict.get("products", []):
                    exact_matches.append((target, "conflicting_template_sku", {"sku": str(value).strip()}))

    derived_targets = list(derived_identity_matches)
    decision = resolve_entity(
        source,
        products,
        entity_type="product",
        exact_matches=exact_matches,
        context_candidates=context_products,
        semantic_candidates=semantic_products,
        source_scope=source_scope,
        sibling_sources=sibling_sources,
        cross_file_targets=[*cross_file_targets, *derived_targets],
        reconciliation=reconciliation,
    )
    if derived_identity_matches:
        candidate = str(decision.get("candidate") or "")
        relevant = (
            derived_identity_matches.get(candidate, [])
            if candidate
            else [proof for proofs in derived_identity_matches.values() for proof in proofs]
        )
        decision["evidence"] = [*decision.get("evidence", []), *relevant]
        decision = finalize_resolution(decision)
    memory_conflict = next((item for item in decision.get("contradictions", []) if item.get("type") == "memory_conflict"), None)
    if memory_conflict:
        for item in memory_items:
            memory.mark_conflicted(str(item.get("memory_id") or ""), current_hard_target=str(memory_conflict.get("current_hard_target") or ""))
    return decision


def resolve_store_evidence(
    source: str,
    stores: Iterable[str],
    memory: LocalMemory,
    run_mappings: dict[str, str],
    *,
    context_stores: Iterable[str] | None = None,
    reconciliation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store_list = list(stores)
    store_by_key = {norm(item): item for item in store_list}
    exact_matches: list[tuple[str, str, dict[str, Any] | None]] = []
    canonical = store_by_key.get(norm(source))
    if canonical:
        exact_matches.append((canonical, "exact_template_store", None))
    run_target = run_mappings.get(f"store:{norm(source)}")
    if run_target:
        exact_matches.append((run_target, "confirmed_run_mapping", {"mapping_type": "store"}))
    memory_item = memory.find("store", source)
    if memory_item:
        exact_matches.append((str(memory_item["target"]), "human_confirmed_local_mapping", {
            "mapping_type": "store", "memory_id": memory_item.get("memory_id"), "lineage_id": memory_item.get("lineage_id"),
        }))
    return resolve_entity(
        source,
        store_list,
        entity_type="store",
        exact_matches=exact_matches,
        context_candidates=context_stores,
        reconciliation=reconciliation,
    )


def resolution_gate(decision: dict[str, Any], gate_type: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    sources = list(decision.get("sources") or [decision.get("source")])
    mapping_sources = list(decision.get("mapping_sources") or sources)
    label = " / ".join(str(item) for item in sources if item)
    entity_type = str(decision.get("entity_type") or "product")
    target_label = "模板店铺" if entity_type == "store" or decision.get("target_kind") == "store" else "模板产品"
    candidate = {
        "entity_type": entity_type,
        "source": mapping_sources[0] if mapping_sources else decision.get("source"),
        "sources": mapping_sources,
        "target": decision.get("candidate"),
    }
    evidence = {"resolution": decision, **(context or {})}
    hint = dict(decision.get("useful_hint") or {})
    if hint.get("evidence_status") == "AMOUNT_ONLY_HINT" and hint.get("candidate"):
        question = (
            f"“{label}”当前缺少独立店铺身份关系，不能自动归属。"
            f"金额上唯一完全匹配的是“{hint['candidate']}”。"
            f"请确认是否属于“{hint['candidate']}”，或提供正确店铺。"
        )
    else:
        question = f"“{label}”应归属哪个{target_label}？"
    return make_gate(
        gate_type,
        "Evidence Resolution Layer could not produce a unique contradiction-free attribution",
        evidence,
        question,
        candidate=candidate,
        persistence=candidate,
    )


def review_decision_key(decision: dict[str, Any]) -> str:
    identity = {
        "entity_type": decision.get("entity_type"),
        "sources": sorted(str(item) for item in (decision.get("sources") or [decision.get("source")]) if item),
        "candidate": decision.get("candidate"),
    }
    stable = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    return "review:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20]


def _review_evidence_summary(decision: dict[str, Any]) -> list[str]:
    classes = set(decision.get("evidence_classes") or [])
    lines = []
    if "SEMANTIC_EVIDENCE" in classes:
        lines.append("名称语义和规格归一化共同支持该产品")
    if "DETERMINISTIC_STRUCTURE" in classes:
        lines.append("当前模板或店铺结构把候选范围限定到该结果")
    if "CROSS_FILE_EVIDENCE" in classes:
        lines.append("其他当前源文件提供了相互印证")
    if "GLOBAL_RECONCILIATION" in classes:
        lines.append("该归属是当前完整费用约束的唯一零差额解")
    if "EXCLUSIVITY_EVIDENCE" in classes:
        lines.append("没有发现同时满足全部约束的其他合理候选")
    reconciliation = decision.get("reconciliation_result") or decision.get("reconciliation") or {}
    if reconciliation.get("status") == "PASS" and not any("零差额" in line for line in lines):
        lines.append("归属后的对账结果通过")
    return lines or ["多项当前运行证据形成唯一首选答案，但不构成强身份事实"]


def make_review_batch(decisions: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    decision_list = list(decisions)
    items = []
    intra_entity_decisions = list(merge_inferred_decisions(decision_list, cross_entity=False))
    merged_decisions = list(merge_inferred_decisions(decision_list, cross_entity=True))
    merged_decisions.sort(key=lambda item: (
        0 if item.get("alternatives") else (1 if item.get("review_risk") == "MEDIUM_REVIEW_RISK" else 2),
        " / ".join(str(value) for value in item.get("sources", [])),
    ))
    for index, decision in enumerate(merged_decisions, 1):
        sources = list(decision.get("sources") or [decision.get("source")])
        proposal = str(decision.get("candidate") or "")
        member_keys = [review_decision_key(item) for item in decision.get("member_decisions", [])] or [review_decision_key(decision)]
        stable = json.dumps([sources, proposal, member_keys], ensure_ascii=False, sort_keys=True)
        review_id = "RV-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:10]
        member_decisions = list(decision.get("member_decisions") or [{
            "entity_type": decision.get("entity_type"),
            "sources": list(decision.get("mapping_sources") or sources),
            "candidate": proposal,
            "target_kind": decision.get("target_kind") or ("store" if decision.get("entity_type") == "store" else "product"),
            "contexts": list(decision.get("contexts") or []),
            "supporting_evidence": list(decision.get("supporting_evidence") or []),
            "run_only": bool(decision.get("run_only")),
        }])
        mapping_sources = list(decision.get("mapping_sources") or sources)
        resolution_candidate = {
            "entity_type": decision.get("entity_type"),
            "sources": mapping_sources,
            "target": proposal,
            "target_kind": decision.get("target_kind") or ("store" if decision.get("entity_type") == "store" else "product"),
            "members": member_decisions,
        }
        item = {
            "number": index,
            "review_id": review_id,
            "status": "PENDING",
            "sources": sources,
            "proposed_answer": proposal,
            "decision_type": INFERRED_REVIEW,
            "evidence_classes": decision.get("evidence_classes", []),
            "evidence_summary": _review_evidence_summary(decision),
            "supporting_evidence": decision.get("supporting_evidence", []),
            "contradictions_checked": decision.get("contradictions_checked"),
            "alternatives": decision.get("alternatives", []),
            "reconciliation_result": decision.get("reconciliation_result"),
            "reason": decision.get("reason"),
            "review_risk": decision.get("review_risk"),
            "contexts": decision.get("contexts", []),
            "human_review_required": True,
            "question": f"我判断：“{'、'.join(sources)}” → “{proposal}”。是否接受？",
            "resolution_candidate": resolution_candidate,
            "member_keys": member_keys,
        }
        persistence_candidates = []
        member_eligibilities = []
        for member in member_decisions:
            member_resolution = {
                "entity_type": member.get("entity_type"),
                "sources": list(member.get("mapping_sources") or member.get("sources") or []),
                "target": proposal,
                "target_kind": member.get("target_kind") or "product",
            }
            member_item = {
                "resolution_candidate": member_resolution,
                "proposed_answer": proposal,
                "contexts": list(member.get("contexts") or []),
                "supporting_evidence": list(member.get("supporting_evidence") or []),
                "run_only": bool(member.get("run_only")),
            }
            eligibility = persistence_eligibility(member_item)
            member_eligibilities.append({**eligibility, "member": member_resolution})
            if eligibility["eligible"]:
                persistence_candidates.append(eligibility)
        item["persistence_eligibility"] = {
            "eligible": bool(persistence_candidates),
            "eligible_member_count": len(persistence_candidates),
            "member_count": len(member_decisions),
            "members": member_eligibilities,
        }
        item["persistence_candidates"] = persistence_candidates
        item["persistence_candidate"] = (
            {"eligible_members": persistence_candidates}
            if persistence_candidates
            else None
        )
        items.append(item)
    if not items:
        return None
    stable = json.dumps([item["review_id"] for item in items], sort_keys=True)
    batch = {
        "batch_id": "RB-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:10],
        "status": "INFERRED_REVIEW",
        "instructions": "可回复“全部接受”，或按编号接受、拒绝、改为实际答案。",
        "items": items,
        "post_intra_entity_family_merge_count": len(intra_entity_decisions),
    }
    batch["review_ux"] = render_review_batch(batch)
    return batch


def finalize_resolution_collection(
    audit: dict[str, Any],
    *,
    human_gates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Refresh every human-facing count after the final resolution list is fixed."""
    resolutions = list(audit.get("resolutions") or [])
    verified = sum(item.get("decision") == VERIFIED for item in resolutions)
    batch = audit.get("review_batch")
    gates = list(human_gates or [])
    if batch:
        batch["review_ux"] = render_review_batch(batch, verified_records=verified, human_gates=gates)
    summary = dict(audit.get("resolution_summary") or {})
    summary.update(
        verified=verified,
        verified_count=verified,
        inferred_review=len((batch or {}).get("items", [])),
        inferred_review_count=len((batch or {}).get("items", [])),
        post_intra_entity_family_merge_count=(batch or {}).get("post_intra_entity_family_merge_count", 0),
    )
    audit["resolution_summary"] = summary
    return audit


def filename_date_evidence(path: Path, internal_dates: set[str]) -> tuple[set[str], str]:
    if internal_dates:
        return internal_dates, "internal"
    return extract_dates_from_name(path.name), "filename_fallback_no_internal_date_column"


def parse_campaign(path: Path) -> dict[str, Any] | None:
    headers, rows, encoding = read_csv_rows(path)
    cost_header = header_index(headers, "花费", "费用", "支出")
    if not cost_header:
        return None
    header_by_key = {norm(header): header for header in headers}
    id_header = next(
        (header_by_key[key] for key in ("记录id", "流水id", "明细id", "计划id", "id") if key in header_by_key),
        None,
    )
    identity_headers = {header: identity_type_for_header(header) for header in headers if identity_type_for_header(header)}
    identity_priority = {"sku": 0, "product_id": 1, "placement_id": 2, "platform_item_id": 3}
    ordered_identity_headers = sorted(identity_headers, key=lambda header: identity_priority.get(str(identity_headers[header]), 99))
    primary_identity_header = ordered_identity_headers[0] if ordered_identity_headers else None
    date_header = header_index(headers, "日期", "时间", "投放日期")
    plan_header = header_index(headers, "计划名称", "推广计划", "计划") or (headers[0] if headers else None)
    product_header = header_index(headers, "商品名称", "产品名称", "投放商品名称")
    records = []
    internal_dates = set()
    for index, row in enumerate(rows, 2):
        if date_header:
            internal_dates |= extract_dates([row.get(date_header)])
        identities = {
            str(identity_headers[header]): str(row.get(header, "") or "").strip()
            for header in ordered_identity_headers
            if str(row.get(header, "") or "").strip()
        }
        primary_type = str(identity_headers.get(primary_identity_header) or "")
        primary_identity = identities.get(primary_type, "")
        records.append({
            "row": index,
            "plan": row.get(plan_header, "") if plan_header else "",
            "record_id": row.get(id_header, "") if id_header else "",
            "sku": primary_identity,
            "identity_type": primary_type,
            "identities": identities,
            "identity_header": primary_identity_header,
            "product_name": row.get(product_header, "") if product_header else "",
            "cost_cents": to_cents(row.get(cost_header, "0")),
            "raw": row,
        })
    dates, date_evidence = filename_date_evidence(path, internal_dates)
    return {
        "kind": "campaign_full_store" if identity_headers else "campaign_regular",
        "path": str(path.resolve()),
        "name": path.name,
        "headers": headers,
        "encoding": encoding,
        "dates": sorted(dates),
        "date_evidence": date_evidence,
        "records": records,
        "identity_header": primary_identity_header,
        "identity_headers": identity_headers,
        "total_cents": sum(item["cost_cents"] for item in records),
    }


def find_financial_table(book: dict[str, Any]) -> dict[str, Any] | None:
    for sheet in book.get("sheets", []):
        values = sheet.get("values", [])
        for header_row, row in enumerate(values[:10]):
            headers = [str(v or "").strip() for v in row]
            required = {
                "transaction": header_index(headers, "流水单号", "交易流水"),
                "store": header_index(headers, "投放账户名称", "店铺名称"),
                "date": header_index(headers, "投放日期", "日期"),
                "type": header_index(headers, "交易类型"),
                "expense": header_index(headers, "支出", "消费金额"),
            }
            if all(required.values()):
                indexes = {name: headers.index(label) for name, label in required.items()}
                records = []
                for r, values_row in enumerate(values[header_row + 1 :], header_row + 2):
                    if not any(v not in (None, "") for v in values_row):
                        continue
                    records.append({
                        "row": r,
                        "transaction_id": str(values_row[indexes["transaction"]] or "").strip(),
                        "store": str(values_row[indexes["store"]] or "").strip(),
                        "date": next(iter(extract_dates([values_row[indexes["date"]]])), ""),
                        "transaction_type": str(values_row[indexes["type"]] or "").strip(),
                        "expense_cents": to_cents(values_row[indexes["expense"]]),
                    })
                return {"sheet": sheet["name"], "records": records, "headers": headers}
    return None


def identify_sales_table(book: dict[str, Any]) -> dict[str, Any] | None:
    for sheet in book.get("sheets", []):
        values = sheet.get("values", [])
        for header_row, row in enumerate(values[:8]):
            headers = [str(v or "").strip() for v in row]
            sku_col = next((i for i, value in enumerate(headers) if norm(value) == "sku" or "skuid" in norm(value)), None)
            if sku_col is None:
                continue
            data_start = header_row + 1
            total_row = None
            for r in range(data_start, min(data_start + 5, len(values))):
                if norm(values[r][sku_col] if sku_col < len(values[r]) else "") in {"合计", "total", "�ϼ�"}:
                    total_row = r
                    data_start = r + 1
                    break
            numeric_candidates = []
            width = max((len(item) for item in values), default=0)
            for col in range(width):
                if col == sku_col:
                    continue
                total_value = values[total_row][col] if total_row is not None and col < len(values[total_row]) else None
                try:
                    total_cents = to_cents(total_value)
                except DailyRoiError:
                    continue
                row_sum = 0
                count = 0
                for data_row in values[data_start:]:
                    sku = str(data_row[sku_col] if sku_col < len(data_row) else "").strip()
                    if not re.fullmatch(r"\d{6,}", sku):
                        continue
                    try:
                        row_sum += to_cents(data_row[col] if col < len(data_row) else 0)
                        count += 1
                    except DailyRoiError:
                        pass
                if count and total_row is not None and row_sum == total_cents and total_cents >= 0:
                    numeric_candidates.append((col, total_cents, count))
            named = next((i for i, value in enumerate(headers) if "成交金额" in str(value) or "销售额" in str(value)), None)
            if named is not None:
                amount_col = named
            elif numeric_candidates:
                amount_col = max(numeric_candidates, key=lambda item: item[1])[0]
            else:
                continue
            date_col = 0
            dates = extract_dates((row[date_col] if row else "" for row in values))
            records = []
            reported = 0
            if total_row is not None:
                reported = to_cents(values[total_row][amount_col])
            for r, data_row in enumerate(values[data_start:], data_start + 1):
                sku = str(data_row[sku_col] if sku_col < len(data_row) else "").strip()
                if not re.fullmatch(r"\d{6,}", sku):
                    continue
                records.append({
                    "row": r,
                    "sku": sku,
                    "amount_cents": to_cents(data_row[amount_col] if amount_col < len(data_row) else 0),
                })
            return {"sheet": sheet["name"], "records": records, "reported_cents": reported, "dates": sorted(dates), "sku_col": sku_col, "amount_col": amount_col}
    return None


def xlsx_layout_snapshot(path: Path) -> dict[str, Any]:
    ns_main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ns_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        result = {"sheets": [], "styles_sha256": hashlib.sha256(archive.read("xl/styles.xml")).hexdigest() if "xl/styles.xml" in archive.namelist() else None}
        for sheet in workbook.find(f"{{{ns_main}}}sheets") or []:
            name = sheet.attrib["name"]
            rel_id = sheet.attrib[f"{{{ns_rel}}}id"]
            target = rel_map[rel_id].lstrip("/")
            if not target.startswith("xl/"):
                target = f"xl/{target}"
            root = ET.fromstring(archive.read(target))
            merges = sorted(item.attrib.get("ref", "") for item in root.findall(f".//{{{ns_main}}}mergeCell"))
            cols = [dict(sorted(item.attrib.items())) for item in root.findall(f".//{{{ns_main}}}col")]
            heights = []
            styles = []
            for row in root.findall(f".//{{{ns_main}}}row"):
                if "ht" in row.attrib or "customHeight" in row.attrib:
                    heights.append({k: row.attrib[k] for k in ("r", "ht", "customHeight") if k in row.attrib})
                for cell in row.findall(f"{{{ns_main}}}c"):
                    if "s" in cell.attrib:
                        styles.append((cell.attrib.get("r"), cell.attrib.get("s")))
            result["sheets"].append({"name": name, "merges": merges, "cols": cols, "row_heights": heights, "cell_styles": styles})
        return result


def compare_layout(before: dict[str, Any], after: dict[str, Any], allowed_style_cells: dict[str, set[str]] | None = None) -> list[str]:
    failures = []
    allowed_style_cells = allowed_style_cells or {}
    if [s["name"] for s in before["sheets"]] != [s["name"] for s in after["sheets"]]:
        failures.append("sheet_names")
        return failures
    for left, right in zip(before["sheets"], after["sheets"]):
        if left["merges"] != right["merges"]:
            failures.append(f"{left['name']}:merges")
        normalize_cols = lambda cols: [{k: v for k, v in item.items() if k != "customWidth"} for item in cols]
        if normalize_cols(left["cols"]) != normalize_cols(right["cols"]):
            failures.append(f"{left['name']}:cols")
        if left["row_heights"] != right["row_heights"]:
            failures.append(f"{left['name']}:row_heights")
        allowed = allowed_style_cells.get(left["name"], set())
        left_styles = {cell: style for cell, style in left["cell_styles"] if cell not in allowed}
        right_styles = {cell: style for cell, style in right["cell_styles"] if cell not in allowed}
        if left_styles != right_styles:
            failures.append(f"{left['name']}:protected_cell_styles")
    return failures


def classify_duplicate_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    proven = []
    suspected = []
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    by_similarity: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for record in records:
        record_id = str(record.get("record_id") or "").strip()
        source_kind = str(record.get("source_kind") or "")
        if record_id:
            identity = (source_kind, record_id)
            if identity in by_identity:
                first = by_identity[identity]
                if first.get("raw") == record.get("raw"):
                    proven.append({"first": first, "duplicate": record, "evidence": "same_nonempty_record_id_and_full_record"})
                else:
                    suspected.append({"first": first, "similar": record, "reason": "same_record_id_conflicting_content"})
            else:
                by_identity[identity] = record
        similarity = (
            norm(record.get("resolved_product")),
            int(record.get("cost_cents") or 0),
            norm(strip_campaign_spec(str(record.get("plan") or ""))),
            str(record.get("date") or ""),
        )
        if similarity[0] and similarity[1] and similarity[2]:
            if similarity in by_similarity:
                first = by_similarity[similarity]
                first_id = str(first.get("record_id") or "")
                if not record_id or not first_id or record_id != first_id:
                    suspected.append({"first": first, "similar": record, "reason": "similarity_without_bottom_level_identity"})
            else:
                by_similarity[similarity] = record
    return proven, suspected


def _main_store_product(store: str, products: list[str], memory: LocalMemory, run_mappings: dict[str, str]) -> str | None:
    suffix = re.split(r"[-—]", store)[-1].strip()
    resolved = resolve_product(suffix, products, memory, run_mappings)
    if resolved:
        return resolved
    suffix = re.sub(r"(结节|贴)$", lambda m: m.group(0), suffix)
    return resolve_product(suffix, products, memory, run_mappings)


def _campaign_dates(campaigns: list[dict[str, Any]]) -> set[str]:
    return {item for campaign in campaigns for item in campaign.get("dates", [])}


def evaluate_dates(
    business_dates: dict[str, list[str]],
    template_date: str | None,
    memory: LocalMemory,
    run_mappings: dict[str, str] | None = None,
) -> tuple[str, bool, list[dict[str, Any]]]:
    gates: list[dict[str, Any]] = []
    all_dates = {value for values in business_dates.values() for value in values}
    if len(all_dates) != 1:
        gates.append(make_gate("HG-02", "Business source dates conflict or are missing", {"source_dates": business_dates}, "业务源文件日期不一致或缺失，请确认目标日期。"))
        return (sorted(all_dates)[-1] if all_dates else ""), False, gates
    target_date = next(iter(all_dates))
    update_template = template_date != target_date
    # Shared Core rule: a single consistent business date outranks a stale
    # template label.  This deterministic relation is not Local Memory.
    return target_date, update_template, gates


def build_golden_payload(
    template: dict[str, Any],
    financial: dict[str, Any],
    campaigns: list[dict[str, Any]],
    sales: dict[str, Any] | None,
    memory: LocalMemory,
    run_mappings: dict[str, str],
    target_date: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    gates: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    unresolved_decisions: list[dict[str, Any]] = []
    review_decisions: list[dict[str, Any]] = []
    products = [item["name"] for item in template["report"]["products"]]
    raw_groups = template.get("store_groups", [])
    group_names = [item["store"] for item in raw_groups]
    group_products: dict[str, set[str]] = {}

    def retain_decision(
        decision: dict[str, Any],
        *,
        gate_type: str | None = None,
        context: dict[str, Any] | None = None,
        mapping_type: str | None = None,
        fact_family: str | None = None,
    ) -> str | None:
        item = dict(decision)
        if mapping_type:
            item["entity_type"] = mapping_type
        if fact_family:
            item["fact_family"] = fact_family
        if gate_type:
            item["gate_type"] = gate_type
        if context:
            item["contexts"] = [context]
        item = finalize_resolution(item)
        review_state = run_mappings.get(review_decision_key(item))
        if item.get("decision") == INFERRED_REVIEW and review_state == "accepted":
            item["evidence"] = [*item.get("evidence", []), {"type": "human_confirmed_review"}]
            item["decision"] = VERIFIED
            item = finalize_resolution(item)
        elif item.get("decision") == INFERRED_REVIEW and review_state == "rejected":
            rejected = str(item.get("candidate") or "")
            item["contradictions"] = [*item.get("contradictions", []), {"type": "human_rejected_proposal", "proposal": rejected}]
            item["alternatives"] = list(dict.fromkeys([*item.get("alternatives", []), rejected]))
            item["candidate"] = None
            item["decision"] = HUMAN_REQUIRED
            item = finalize_resolution(item)
        resolutions.append(item)
        if item.get("decision") == HUMAN_REQUIRED:
            unresolved_decisions.append(item)
            return None
        if item.get("decision") == INFERRED_REVIEW:
            review_decisions.append(item)
        return str(item.get("candidate") or "") or None

    def merge_store_split(store: str, file_name: str, split: dict[str, int]) -> None:
        current = store_splits.setdefault(store, {"detail_files": [], "products": {}, "split_total_cents": 0})
        if file_name not in current["detail_files"]:
            current["detail_files"].append(file_name)
        for product, amount in split.items():
            current["products"][product] = current["products"].get(product, 0) + amount
        current["split_total_cents"] = sum(current["products"].values())

    for group in raw_groups:
        resolved = set()
        for member in group.get("products", []):
            decision = resolve_product_evidence(member, template, memory, run_mappings, context_products=products)
            product = retain_decision(
                decision,
                gate_type="HG-06",
                context={"template_store": group["store"], "template_member": member},
            )
            if product:
                resolved.add(product)
        group_products[group["store"]] = resolved

    ledger_records = [
        item for item in financial["records"]
        if item["date"] == target_date and "快车扣费" in item["transaction_type"]
    ]
    transaction_ids: dict[str, dict[str, Any]] = {}
    for item in ledger_records:
        tx = item["transaction_id"]
        if tx and tx in transaction_ids:
            gates.append(make_gate(
                "HG-03", "Duplicate financial transaction id requires confirmation", {"transaction_id": tx, "rows": [transaction_ids[tx]["row"], item["row"]]},
                f"财务流水 {tx} 出现两次。请确认是否为同一底层交易。",
            ))
        elif tx:
            transaction_ids[tx] = item
    ledger_total = sum(item["expense_cents"] for item in ledger_records)
    ledger_by_store = defaultdict(int)
    for item in ledger_records:
        ledger_by_store[item["store"]] += item["expense_cents"]

    components: dict[str, list[dict[str, Any]]] = {product: [] for product in products}
    store_splits: dict[str, dict[str, Any]] = {}
    detailed_stores: set[str] = set()
    audit_campaign_records: list[dict[str, Any]] = []

    regular = [item for item in campaigns if item["kind"] == "campaign_regular"]
    full_store = [item for item in campaigns if item["kind"] == "campaign_full_store"]
    identity_index = build_product_identity_index(template, campaigns)

    sibling_plans: dict[str, list[str]] = defaultdict(list)
    for campaign in regular:
        for record in campaign["records"]:
            if record["cost_cents"] and norm(record["plan"]) not in {"单盒", "三盒", "1", "3", "一盒", "3盒", "1盒"}:
                sibling_plans[alias_family(record["plan"])].append(record["plan"])

    cross_file_hints: dict[str, set[str]] = defaultdict(set)
    for campaign in campaigns:
        for record in campaign["records"]:
            identity_targets = {
                mapped["product"]
                for value in record_identity_values(record).values()
                for mapped in identity_index.get(value, [])
            }
            family = alias_family(record.get("plan"))
            if record["cost_cents"] and len(identity_targets) == 1 and family:
                cross_file_hints[family].add(next(iter(identity_targets)))

    canonical_ledger: dict[str, dict[str, Any]] = {}
    for raw_store, amount in ledger_by_store.items():
        store_decision = resolve_store_evidence(raw_store, group_names, memory, run_mappings) if group_names else None
        canonical = None
        if store_decision and store_decision.get("decision") != HUMAN_REQUIRED:
            canonical = retain_decision(store_decision)
        if canonical:
            entry = canonical_ledger.setdefault(canonical, {"raw_stores": [], "amount_cents": 0})
            entry["raw_stores"].append(raw_store)
            entry["amount_cents"] += amount
            continue

        suffix = re.split(r"[-—]", raw_store)[-1].strip()
        product_decision = resolve_product_evidence(
            suffix,
            template,
            memory,
            run_mappings,
            sibling_sources=sibling_plans.get(alias_family(suffix), []),
            cross_file_targets=cross_file_hints.get(alias_family(suffix), set()),
        )
        product = None
        if product_decision.get("decision") != HUMAN_REQUIRED:
            product = retain_decision(product_decision)
        elif amount:
            chosen = product_decision
            if store_decision and (store_decision.get("candidate") or store_decision.get("alternatives")) and not (
                product_decision.get("candidate") or product_decision.get("alternatives")
            ):
                chosen = store_decision
            retain_decision(
                chosen,
                gate_type="HG-01",
                context={"store": raw_store, "source_product": suffix, "amount": cents_text(amount)},
            )
        if product and amount:
            components[product].insert(0, {"cents": amount, "source_file": financial["source_name"], "kind": "financial_single_store", "store": raw_store})

    global_facts = []
    for campaign in campaigns:
        resolved_products: set[str] = set()
        has_conflict = False
        for record in campaign["records"]:
            if not record["cost_cents"]:
                continue
            identities = record_identity_values(record)
            source = record.get("product_name") or next(iter(identities.values()), "") or record.get("plan") or ""
            decision = resolve_product_evidence(
                source,
                template,
                memory,
                run_mappings,
                identity_values=identities,
                identity_index=identity_index,
                semantic_products=[],
                source_scope="deterministic_identity_only",
            )
            if decision.get("contradictions"):
                has_conflict = True
            if decision.get("decision") != HUMAN_REQUIRED and decision.get("candidate"):
                resolved_products.add(str(decision["candidate"]))
        candidate_stores = [
            store_name
            for store_name, members in group_products.items()
            if not resolved_products or resolved_products.issubset(members)
        ]
        if campaign["kind"] == "campaign_regular":
            families = sorted({alias_family(item["plan"]) for item in campaign["records"] if item["cost_cents"] and alias_family(item["plan"])})
            assignment_token = "regular_store:" + hashlib.sha256(json.dumps(families, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        else:
            assignment_token = "full_store_export"
        assigned_target = run_mappings.get(f"campaign:{norm(assignment_token)}") or memory.resolve("campaign", assignment_token)
        assigned_store = resolve_store(assigned_target or "", group_names, memory, run_mappings) if assigned_target else None
        if assigned_target and not assigned_store:
            has_conflict = True
        elif assigned_store:
            if assigned_store not in candidate_stores:
                has_conflict = True
            else:
                candidate_stores = [assigned_store]
        if campaign["total_cents"]:
            global_facts.append({
                "id": str(id(campaign)),
                "source": campaign["name"],
                "total_cents": campaign["total_cents"],
                "candidate_stores": candidate_stores,
                "products": sorted(resolved_products),
                "has_conflict": has_conflict,
            })
    global_constraints = resolve_global_store_constraints(
        global_facts,
        {store: canonical_ledger.get(store, {}).get("amount_cents", 0) for store in group_names},
    )
    global_store_decisions = dict(global_constraints.get("decisions") or {})
    amount_only_hint = dict(global_constraints.get("amount_only_hint") or {})
    amount_only_hint_assignments = dict(amount_only_hint.get("assignments") or {})

    for campaign in regular:
        regular_families = sorted({alias_family(item["plan"]) for item in campaign["records"] if item["cost_cents"] and alias_family(item["plan"])})
        regular_store_token = "regular_store:" + hashlib.sha256(json.dumps(regular_families, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        assigned_store = run_mappings.get(f"campaign:{norm(regular_store_token)}") or memory.resolve("campaign", regular_store_token)
        assigned_store = resolve_store(assigned_store or "", group_names, memory, run_mappings) if assigned_store else None
        global_store_decision = global_store_decisions.get(str(id(campaign)))
        global_store = str((global_store_decision or {}).get("candidate") or "") or None
        if assigned_store and global_store and assigned_store != global_store:
            conflict = resolve_entity(
                regular_store_token,
                group_names,
                entity_type="store",
                exact_matches=[
                    (assigned_store, "human_confirmed_store_assignment", None),
                    (global_store, "global_constraint_unique_solution", None),
                ],
            )
            conflict["entity_type"] = "campaign"
            conflict["mapping_sources"] = [regular_store_token]
            conflict["target_kind"] = "store"
            retain_decision(conflict, gate_type="HG-06", context={"file": campaign["name"]})
            continue
        store = assigned_store or global_store
        if global_store_decision and not assigned_store:
            global_store_decision = dict(global_store_decision)
            global_store_decision.update(
                entity_type="campaign",
                mapping_sources=[regular_store_token],
                target_kind="store",
            )
            retain_decision(global_store_decision)
        provisional: list[dict[str, Any]] = []
        scoped_products = group_products.get(store, set()) if store else set()
        for record in campaign["records"]:
            plan = record["plan"]
            generic = norm(plan) in {"单盒", "三盒", "1", "3", "一盒", "3盒", "1盒"}
            decision = None
            if not generic and record["cost_cents"]:
                decision = resolve_product_evidence(
                    plan,
                    template,
                    memory,
                    run_mappings,
                    identity_values=record_identity_values(record),
                    identity_index=identity_index,
                    context_products=scoped_products if scoped_products else None,
                    semantic_products=None if scoped_products else [],
                    source_scope="current_store_products" if scoped_products else "deterministic_identity_only",
                    sibling_sources=sibling_plans.get(alias_family(plan), []),
                    cross_file_targets=cross_file_hints.get(alias_family(plan), set()),
                )
            provisional.append({"record": record, "generic": generic, "decision": decision})
        if campaign["total_cents"] == 0:
            continue

        first_products = {
            str(item["decision"].get("candidate"))
            for item in provisional
            if item["decision"] and item["decision"].get("decision") != HUMAN_REQUIRED and item["record"]["cost_cents"]
        }
        candidates = [store for store, members in group_products.items() if first_products and first_products.issubset(members)]
        reconciled_candidates = [
            store for store in candidates
            if canonical_ledger.get(store, {}).get("amount_cents") == campaign["total_cents"]
        ]
        store = store or (candidates[0] if len(candidates) == 1 else (reconciled_candidates[0] if len(reconciled_candidates) == 1 else None))
        if store:
            reconciliation = {
                "status": "PASS",
                "store": store,
                "ledger_total_cents": canonical_ledger.get(store, {}).get("amount_cents"),
                "detail_total_cents": campaign["total_cents"],
            } if canonical_ledger.get(store, {}).get("amount_cents") == campaign["total_cents"] else {"status": "NOT_TESTED"}
            for item in provisional:
                if item["generic"] or not item["decision"] or item["decision"].get("decision") != HUMAN_REQUIRED:
                    continue
                plan = item["record"]["plan"]
                item["decision"] = resolve_product_evidence(
                    plan,
                    template,
                    memory,
                    run_mappings,
                    identity_values=record_identity_values(item["record"]),
                    identity_index=identity_index,
                    context_products=group_products.get(store, set()) or None,
                    semantic_products=None if group_products.get(store, set()) else [],
                    source_scope="current_store_products" if group_products.get(store, set()) else "deterministic_identity_only",
                    sibling_sources=sibling_plans.get(alias_family(plan), []),
                    cross_file_targets=cross_file_hints.get(alias_family(plan), set()),
                    reconciliation=reconciliation,
                )

        unresolved_rows = []
        non_generic_products: set[str] = set()
        for item in provisional:
            if item["generic"] or not item["decision"]:
                continue
            record = item["record"]
            decision = item["decision"]
            if decision.get("decision") == HUMAN_REQUIRED:
                decision["entity_type"] = "campaign"
                decision["fact_family"] = f"campaign:{alias_family(record['plan'])}"
                if record["cost_cents"]:
                    unresolved_rows.append(item)
                    retain_decision(
                        decision,
                        gate_type="HG-06",
                        context={"file": campaign["name"], "row": record["row"], "campaign": record["plan"], "amount": cents_text(record["cost_cents"]), "store": store},
                    )
            else:
                product = retain_decision(decision, mapping_type="campaign", fact_family=f"campaign:{alias_family(record['plan'])}")
                if product and record["cost_cents"]:
                    non_generic_products.add(product)
        if unresolved_rows:
            continue

        candidates = [store_name for store_name, members in group_products.items() if non_generic_products and non_generic_products.issubset(members)]
        reconciled_candidates = [
            store_name for store_name in candidates
            if canonical_ledger.get(store_name, {}).get("amount_cents") == campaign["total_cents"]
        ]
        store = store or (candidates[0] if len(candidates) == 1 else (reconciled_candidates[0] if len(reconciled_candidates) == 1 else None))
        if not store:
            decision = resolve_entity(campaign["name"], group_names, entity_type="store", context_candidates=candidates)
            decision["fact_family"] = f"store-file:{norm(campaign['name'])}"
            decision["alternatives"] = candidates
            decision["entity_type"] = "campaign"
            decision["mapping_sources"] = [regular_store_token]
            decision["target_kind"] = "store"
            hinted_store = amount_only_hint_assignments.get(str(id(campaign)))
            if hinted_store:
                decision["useful_hint"] = {
                    "evidence_status": "AMOUNT_ONLY_HINT",
                    "candidate": hinted_store,
                    "selected_answer": None,
                }
            retain_decision(
                decision,
                gate_type="HG-06",
                context={"file": campaign["name"], "resolved_products": sorted(non_generic_products), "candidate_stores": candidates, "amount": cents_text(campaign["total_cents"])},
            )
            continue
        store_decision = resolve_entity(
            campaign["name"],
            group_names,
            entity_type="store",
            exact_matches=[(store, "unique_store_product_membership", {"products": sorted(non_generic_products)})],
            reconciliation={
                "status": "PASS" if canonical_ledger.get(store, {}).get("amount_cents") == campaign["total_cents"] else "NOT_TESTED",
                "store": store,
            },
        )
        retain_decision(store_decision)
        detailed_stores.add(store)
        split = defaultdict(int)
        generic_nonzero = [item for item in provisional if item["generic"] and item["record"]["cost_cents"]]
        main_decision = None
        main_product = None
        if generic_nonzero:
            suffix = re.split(r"[-—]", store)[-1].strip()
            main_decision = resolve_product_evidence(
                suffix,
                template,
                memory,
                run_mappings,
                context_products=group_products.get(store, set()),
                cross_file_targets=cross_file_hints.get(alias_family(suffix), set()),
            )
            main_decision["fact_family"] = f"product:store-main:{norm(store)}"
            main_product = retain_decision(
                main_decision,
                gate_type="HG-06",
                context={"store": store, "generic_plans": [item["record"]["plan"] for item in generic_nonzero]},
            )
            if not main_product:
                continue
        for item in provisional:
            record = item["record"]
            resolved = None
            if item["generic"]:
                if not record["cost_cents"]:
                    continue
                generic_decision = resolve_entity(
                    record["plan"],
                    products,
                    entity_type="campaign",
                    exact_matches=[(main_product, "generic_plan_to_unique_store_main_product", {"store": store})],
                )
                resolved = retain_decision(generic_decision, mapping_type="campaign", fact_family=f"campaign:{norm(store)}:generic-main")
            elif item["decision"]:
                resolved = str(item["decision"].get("candidate") or "")
            if not resolved:
                continue
            split[resolved] += record["cost_cents"]
            if record["cost_cents"]:
                component = {"cents": record["cost_cents"], "source_file": campaign["name"], "row": record["row"], "kind": "campaign_regular", "store": store, "plan": record["plan"], "record_id": record["record_id"]}
                components[resolved].append(component)
                audit_campaign_records.append({**record, "resolved_product": resolved, "source_kind": campaign["kind"], "source_file": campaign["name"], "date": target_date})
        merge_store_split(store, campaign["name"], dict(split))

    for campaign in full_store:
        source_token = "full_store_export"
        assigned = run_mappings.get(f"campaign:{norm(source_token)}") or memory.resolve("campaign", source_token)
        assigned_store = resolve_store(assigned or "", group_names, memory, run_mappings) if assigned else None
        global_store_decision = global_store_decisions.get(str(id(campaign)))
        global_store = str((global_store_decision or {}).get("candidate") or "") or None
        if assigned_store and global_store and assigned_store != global_store:
            conflict = resolve_entity(
                source_token,
                group_names,
                entity_type="store",
                exact_matches=[
                    (assigned_store, "human_confirmed_store_assignment", None),
                    (global_store, "global_constraint_unique_solution", None),
                ],
            )
            conflict["entity_type"] = "campaign"
            conflict["target_kind"] = "store"
            retain_decision(conflict, gate_type="HG-06", context={"file": campaign["name"]})
            continue
        store = assigned_store or global_store
        if global_store_decision and not assigned_store:
            global_store_decision = dict(global_store_decision)
            global_store_decision.update(
                entity_type="campaign",
                mapping_sources=[source_token],
                target_kind="store",
            )
            retain_decision(global_store_decision)
        split = defaultdict(int)
        record_decisions = []
        scoped_products = group_products.get(store, set()) if store else set()
        for record in campaign["records"]:
            identities = record_identity_values(record)
            source = record.get("product_name") or next(iter(identities.values()), "") or record.get("plan") or ""
            decision = resolve_product_evidence(
                source,
                template,
                memory,
                run_mappings,
                identity_values=identities,
                identity_index=identity_index,
                context_products=scoped_products if scoped_products else None,
                semantic_products=None if scoped_products else [],
                source_scope="current_store_products" if scoped_products else "deterministic_identity_only",
                sibling_sources=sibling_plans.get(alias_family(record.get("plan")), []),
                cross_file_targets=cross_file_hints.get(alias_family(record.get("plan")), set()),
            )
            decision["entity_type"] = "product"
            decision["fact_family"] = f"product-identity:{record.get('identity_type') or 'unknown'}:{norm(source)}"
            record_decisions.append((record, decision))
        unresolved = [item for item in record_decisions if item[0]["cost_cents"] and item[1].get("decision") == HUMAN_REQUIRED]
        for record, decision in record_decisions:
            resolved = retain_decision(
                decision,
                gate_type="HG-05" if record["cost_cents"] and decision.get("decision") == HUMAN_REQUIRED else None,
                context={"file": campaign["name"], "row": record["row"], "identities": record_identity_values(record), "amount": cents_text(record["cost_cents"])} if record["cost_cents"] else None,
            )
            if not resolved or not record["cost_cents"]:
                continue
            split[resolved] += record["cost_cents"]
        if unresolved:
            continue
        resolved_products = set(split)
        if not store:
            candidates = [store_name for store_name, members in group_products.items() if resolved_products and resolved_products.issubset(members)]
            reconciled_candidates = [
                store_name for store_name in candidates
                if canonical_ledger.get(store_name, {}).get("amount_cents") == campaign["total_cents"]
            ]
            store = candidates[0] if len(candidates) == 1 else (reconciled_candidates[0] if len(reconciled_candidates) == 1 else None)
            if store:
                decision = resolve_entity(
                    source_token,
                    group_names,
                    entity_type="store",
                    exact_matches=[(store, "unique_store_product_membership", {"products": sorted(resolved_products)})],
                    reconciliation={"status": "PASS" if store in reconciled_candidates else "NOT_TESTED", "store": store},
                )
                retain_decision(decision)
            elif campaign["total_cents"]:
                decision = resolve_entity(source_token, group_names, entity_type="store", context_candidates=candidates)
                decision["entity_type"] = "campaign"
                decision["fact_family"] = f"campaign:{source_token}"
                decision["alternatives"] = candidates
                decision["target_kind"] = "store"
                decision["source"] = campaign["name"]
                decision["sources"] = [campaign["name"]]
                decision["mapping_sources"] = [source_token]
                hinted_store = amount_only_hint_assignments.get(str(id(campaign)))
                if hinted_store:
                    decision["useful_hint"] = {
                        "evidence_status": "AMOUNT_ONLY_HINT",
                        "candidate": hinted_store,
                        "selected_answer": None,
                    }
                retain_decision(
                    decision,
                    gate_type="HG-06",
                    context={"file": campaign["name"], "amount": cents_text(campaign["total_cents"]), "schema": "full_store_identity_export", "resolved_products": sorted(resolved_products)},
                )
                continue
        if store:
            detailed_stores.add(store)
            for record, decision in record_decisions:
                resolved = str(decision.get("candidate") or "")
                if not resolved or not record["cost_cents"]:
                    continue
                component = {"cents": record["cost_cents"], "source_file": campaign["name"], "row": record["row"], "kind": "campaign_full_store", "store": store, "identities": record_identity_values(record)}
                components[resolved].append(component)
                audit_campaign_records.append({**record, "resolved_product": resolved, "source_kind": campaign["kind"], "source_file": campaign["name"], "date": target_date})
            merge_store_split(store, campaign["name"], dict(split))

    proven, suspected = classify_duplicate_records(audit_campaign_records)
    duplicate_record_keys = {(item["duplicate"]["source_file"], item["duplicate"]["row"]) for item in proven}
    if duplicate_record_keys:
        for product in components:
            components[product] = [item for item in components[product] if (item["source_file"], item["row"]) not in duplicate_record_keys]
    for item in suspected:
        gates.append(make_gate(
            "HG-03", "Records are similar but bottom-level identity is not proven", {"first": {k: item["first"].get(k) for k in ("source_file", "row", "plan", "record_id", "cost_cents")}, "similar": {k: item["similar"].get(k) for k in ("source_file", "row", "plan", "record_id", "cost_cents")}},
            "这两条消费记录相似但无法证明是同一底层事件。请确认是否重复。",
        ))

    mapping_blocked = bool(unresolved_decisions) or any(item["gate_type"] in {"HG-01", "HG-03", "HG-05", "HG-06"} for item in gates)
    reconciliations = []
    for store in group_names:
        ledger = canonical_ledger.get(store, {"raw_stores": [store], "amount_cents": 0})
        raw_store = " / ".join(ledger["raw_stores"])
        ledger_amount = ledger["amount_cents"]
        split = store_splits.get(store, {"products": {}, "split_total_cents": 0, "detail_files": []})
        difference = split["split_total_cents"] - ledger_amount
        reconciliations.append({
            "store": store,
            "ledger_store": raw_store,
            "ledger_total_cents": ledger_amount,
            "product_splits_cents": split["products"],
            "split_total_cents": split["split_total_cents"],
            "difference_cents": difference,
            "detail_files": split["detail_files"],
        })
        if difference and not mapping_blocked:
            gates.append(make_gate(
                "HG-04", "Multi-product store detail does not reconcile to the financial ledger", {"store": store, "ledger_total": cents_text(ledger_amount), "split_total": cents_text(split["split_total_cents"]), "difference": cents_text(difference), "files": split["detail_files"]},
                f"店铺 {store} 拆分与总表相差 {cents_text(difference)}，请检查缺失或重复明细。",
            ))

    product_expenses = []
    product_total = 0
    for product in products:
        parts = components[product]
        values = [item["cents"] for item in parts]
        product_total += sum(values)
        product_expenses.append({"product": product, "components_cents": values, "components": parts, "total_cents": sum(values)})
    expense_difference = product_total - ledger_total
    if expense_difference and not mapping_blocked:
        gates.append(make_gate(
            "HG-04", "Product expense total does not reconcile to financial ledger", {"ledger_total": cents_text(ledger_total), "product_total": cents_text(product_total), "difference": cents_text(expense_difference)},
            f"产品费用合计与总消费相差 {cents_text(expense_difference)}，请确认数据来源。",
        ))

    sales_payload = {"write": False, "reason": "sales_or_explicit_brushing_input_absent"}
    sales_audit = None
    if sales and template.get("sku"):
        sku_map = template["sku"]["map"]
        by_product = {product: {"single": [], "triple": [], "other": []} for product in products}
        source_total = 0
        source_skus = set()
        for record in sales["records"]:
            source_skus.add(record["sku"])
            source_total += record["amount_cents"]
            mapped = sku_map.get(record["sku"])
            if not mapped:
                decision = resolve_product_evidence(record["sku"], template, memory, run_mappings, identity_value=record["sku"])
                decision["entity_type"] = "sku"
                decision["fact_family"] = f"sku:{norm(record['sku'])}"
                confirmed_product = retain_decision(
                    decision,
                    gate_type="HG-05" if record["amount_cents"] and decision.get("decision") == HUMAN_REQUIRED else None,
                    context={"row": record["row"], "sku": record["sku"], "amount": cents_text(record["amount_cents"])} if record["amount_cents"] else None,
                )
                if confirmed_product:
                    mapped = {"product": confirmed_product, "spec": "other"}
            if not mapped:
                continue
            spec = mapped.get("spec") if mapped.get("spec") in {"single", "triple"} else "other"
            by_product[mapped["product"]][spec].append(record["amount_cents"])
        template_skus = set(sku_map)
        missing = sorted(template_skus - source_skus)
        if missing:
            gates.append(make_gate(
                "HG-05", "Sales file does not cover every template SKU", {"missing_skus": missing, "matched_count": len(template_skus & source_skus), "expected_count": len(template_skus)},
                "销售文件缺少模板 SKU，请确认是否使用了完整导出。",
            ))
        if source_total != sales["reported_cents"]:
            gates.append(make_gate(
                "HG-04", "Sales SKU sum does not equal the sales file reported total", {"reported": cents_text(sales["reported_cents"]), "sku_sum": cents_text(source_total), "difference": cents_text(source_total - sales["reported_cents"])},
                "销售 SKU 汇总与文件合计不一致，请确认源文件。",
            ))
        brush_values = {item["name"]: to_cents(item.get("brush_value")) for item in template["sku"]["products"]}
        explicit_brushing = any(value != 0 for value in brush_values.values())
        if explicit_brushing:
            brush_total = sum(brush_values.values())
            sales_products = []
            for product in products:
                grouped = by_product[product]
                parts = grouped["single"] + grouped["triple"] + grouped["other"]
                sales_products.append({"product": product, "components_cents": parts, "gross_cents": sum(parts), "brush_cents": brush_values.get(product, 0), "real_cents": sum(parts) - brush_values.get(product, 0)})
            sales_payload = {
                "write": True,
                "products": sales_products,
                "gross_sales_cents": source_total,
                "brushing_cents": brush_total,
                "real_sales_cents": source_total - brush_total,
            }
        sales_audit = {"reported_cents": sales["reported_cents"], "sku_sum_cents": source_total, "matched_count": len(template_skus & source_skus), "expected_template_sku_count": len(template_skus), "external_skus": sorted(source_skus - template_skus), "missing_skus": missing}

    resolutions = union_cross_path_evidence(resolutions)
    for decision in resolutions:
        if decision.get("decision") != INFERRED_REVIEW:
            continue
        review_state = run_mappings.get(review_decision_key(decision))
        if review_state == "rejected":
            proposal = str(decision.get("candidate") or "")
            decision["contradictions"] = [
                *decision.get("contradictions", []),
                {"type": "human_rejected_proposal", "proposal": proposal},
            ]
            decision["alternatives"] = list(dict.fromkeys([*decision.get("alternatives", []), proposal]))
            decision["candidate"] = None
            decision["decision"] = HUMAN_REQUIRED
            decision.update(finalize_resolution(decision))
    unresolved_decisions = [item for item in resolutions if item.get("decision") == HUMAN_REQUIRED]
    review_decisions = [item for item in resolutions if item.get("decision") == INFERRED_REVIEW]

    for decision in merge_human_decisions(unresolved_decisions):
        gate_types = set(decision.get("gate_types", []))
        gate_type = "HG-05" if "HG-05" in gate_types else ("HG-01" if "HG-01" in gate_types else "HG-06")
        gates.append(resolution_gate(decision, gate_type, context={"occurrences": decision.get("contexts", [])}))

    payload = {
        "schema_version": 1,
        "target_date": target_date,
        "update_template_date": template["report"].get("template_date") != target_date,
        "product_expenses": product_expenses,
        "expense_total_cents": product_total,
        "sales": sales_payload,
        "roi_number_format": "0.00",
    }
    review_batch = make_review_batch(review_decisions)
    merged_human = merge_human_decisions(unresolved_decisions)
    audit = {
        "target_date": target_date,
        "financial_total_cents": ledger_total,
        "product_expense_total_cents": product_total,
        "expense_difference_cents": expense_difference,
        "multi_store_reconciliations": reconciliations,
        "proven_duplicates": [{"evidence": item["evidence"], "source_file": item["duplicate"]["source_file"], "row": item["duplicate"]["row"]} for item in proven],
        "suspected_duplicates": len(suspected),
        "sales": sales_audit,
        "resolutions": resolutions,
        "review_batch": review_batch,
        "resolution_summary": {
            "verified": 0,
            "machine_inferred": 0,
            "inferred_review": len((review_batch or {}).get("items", [])),
            "human_required": len(merged_human),
            "verified_count": 0,
            "inferred_review_count": len((review_batch or {}).get("items", [])),
            "human_required_count": len(merged_human),
            "open_ended_human_decisions": len(merged_human),
        },
    }
    finalize_resolution_collection(audit, human_gates=gates)
    return payload, audit, gates


def run_report(
    workspace: Path,
    input_dir: Path,
    output_dir: Path,
    *,
    node: str | None = None,
    node_modules: str | None = None,
    existing_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dependencies = dependency_preflight(input_dir, node=node, node_modules=node_modules)
    paths = RuntimePaths.for_workspace(workspace)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.runs_dir.mkdir(parents=True, exist_ok=True)
    memory = LocalMemory(paths)
    bridge = WorkbookBridge(Path(dependencies["node"]), Path(dependencies["node_modules"]))
    run_id = existing_state.get("run_id") if existing_state else datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run_dir = paths.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run_mappings = dict((existing_state or {}).get("run_mappings", {}))

    files = sorted((p for p in input_dir.iterdir() if p.is_file()), key=lambda p: p.name)
    if not files:
        raise DailyRoiError("Input directory contains no files")
    manifest = [{"name": p.name, "path": str(p.resolve()), "bytes": p.stat().st_size, "sha256": sha256_file(p), "extension": p.suffix.lower()} for p in files]
    hash_groups = defaultdict(list)
    for item in manifest:
        hash_groups[item["sha256"]].append(item["name"])
    exact_duplicate_files = [names for names in hash_groups.values() if len(names) > 1]

    templates = []
    sales_sources = []
    financial_sources = []
    campaigns = []
    unclassified = []
    for source in files:
        suffix = source.suffix.lower()
        if suffix == ".csv":
            parsed = parse_campaign(source)
            if parsed:
                campaigns.append(parsed)
            else:
                unclassified.append(source.name)
        elif suffix == ".xls":
            converted = convert_xls(source, run_dir)
            inspected = bridge.inspect_xlsx(converted)
            financial = find_financial_table(inspected)
            if financial:
                financial.update({"source_path": str(source.resolve()), "source_name": source.name, "converted_path": str(converted)})
                financial_sources.append(financial)
            else:
                unclassified.append(source.name)
        elif suffix == ".xlsx":
            try:
                template = bridge.inspect_template(source)
            except DailyRoiError:
                template = None
            if template:
                template["source_path"] = str(source.resolve())
                templates.append(template)
            else:
                inspected = bridge.inspect_xlsx(source)
                financial = find_financial_table(inspected)
                sales = identify_sales_table(inspected)
                if financial:
                    financial.update({"source_path": str(source.resolve()), "source_name": source.name})
                    financial_sources.append(financial)
                elif sales:
                    sales.update({"source_path": str(source.resolve()), "source_name": source.name})
                    sales_sources.append(sales)
                else:
                    unclassified.append(source.name)
        else:
            unclassified.append(source.name)

    gates: list[dict[str, Any]] = []
    if len(templates) != 1:
        gates.append(make_gate("HG-06", "Template classification is not unique", {"template_candidates": [item.get("source_path") for item in templates], "unclassified": unclassified}, "无法唯一识别日报模板，请确认模板文件。"))
    if len(financial_sources) != 1:
        gates.append(make_gate("HG-06", "Financial source classification is not unique", {"financial_candidates": [item.get("source_path") for item in financial_sources], "unclassified": unclassified}, "无法唯一识别财务消费总表，请确认。"))
    if len(sales_sources) > 1:
        gates.append(make_gate("HG-06", "Multiple sales sources were identified", {"sales_candidates": [item.get("source_path") for item in sales_sources]}, "识别到多份销售文件，请确认使用哪一份。"))
    if exact_duplicate_files:
        # Exact file hashes prove identical exports. The first lexical file is kept and the decision is audited.
        duplicate_names = {name for group in exact_duplicate_files for name in group[1:]}
        campaigns = [item for item in campaigns if item["name"] not in duplicate_names]

    business_dates: dict[str, list[str]] = {}
    if financial_sources:
        business_dates["financial"] = sorted({item["date"] for item in financial_sources[0]["records"] if item["date"]})
    if sales_sources:
        business_dates["sales"] = sales_sources[0]["dates"]
    for campaign in campaigns:
        business_dates[f"campaign:{campaign['name']}"] = campaign["dates"]
    all_dates = {value for values in business_dates.values() for value in values}
    target_date, _, date_gates = evaluate_dates(
        business_dates,
        templates[0]["report"].get("template_date") if len(templates) == 1 else None,
        memory,
        run_mappings,
    )
    gates.extend(date_gates)

    payload = None
    audit = None
    if templates and financial_sources and target_date and len(all_dates) == 1:
        computed_payload, computed_audit, computed_gates = build_golden_payload(
            templates[0], financial_sources[0], campaigns, sales_sources[0] if sales_sources else None, memory, run_mappings, target_date
        )
        payload, audit = computed_payload, computed_audit
        gates.extend(computed_gates)
        template_date = templates[0]["report"].get("template_date")
        if template_date != target_date and not date_gates:
            date_decision = finalize_resolution({
                "schema_version": 1,
                "entity_type": "workflow",
                "source": str(template_date or ""),
                "sources": [str(template_date or "")],
                "fact_family": f"workflow:{WORKFLOW_RULE_STALE_TEMPLATE}",
                "candidate": target_date,
                "decision": VERIFIED,
                "evidence": [
                    {"type": "consistent_business_dates_template_only_stale", "business_date": target_date, "template_date": template_date},
                    {"type": "shared_core_deterministic_rule", "rule": WORKFLOW_RULE_STALE_TEMPLATE},
                ],
                "contradictions": [],
                "alternatives": [],
                "candidate_generation": [],
                "reconciliation": {"status": "PASS", "source_dates": business_dates},
            })
            audit["resolutions"].append(date_decision)

    unique_gates = {gate["gate_id"]: gate for gate in gates}
    if audit is not None:
        finalize_resolution_collection(audit, human_gates=list(unique_gates.values()))
    review_batch = (audit or {}).get("review_batch")
    pending_reviews = list((review_batch or {}).get("items", []))
    if unique_gates:
        run_status = "HUMAN_REQUIRED"
    elif pending_reviews:
        run_status = "INFERRED_REVIEW"
    else:
        run_status = "READY_TO_WRITE"
    review_metrics = dict((existing_state or {}).get("review_metrics") or {})
    review_metrics.setdefault("review_accept_count", 0)
    review_metrics.setdefault("review_reject_count", 0)
    review_metrics.setdefault("review_correct_count", 0)
    state = {
        "schema_version": 1,
        "run_id": run_id,
        "status": run_status,
        "stage": "RESOLVE" if unique_gates or pending_reviews else "RECONCILE",
        "created_at": (existing_state or {}).get("created_at", now_iso()),
        "updated_at": now_iso(),
        "workspace": str(paths.workspace),
        "input_dir": str(input_dir.resolve()),
        "output_dir": str(output_dir.resolve()),
        "manifest": manifest,
        "classification": {
            "template": templates[0]["source_path"] if len(templates) == 1 else None,
            "financial": financial_sources[0]["source_path"] if len(financial_sources) == 1 else None,
            "sales": sales_sources[0]["source_path"] if len(sales_sources) == 1 else None,
            "campaigns": [{"name": item["name"], "kind": item["kind"], "total_cents": item["total_cents"], "dates": item["dates"], "date_evidence": item["date_evidence"]} for item in campaigns],
            "unclassified": unclassified,
        },
        "target_date": target_date,
        "template_model": templates[0] if len(templates) == 1 else None,
        "exact_duplicate_files": exact_duplicate_files,
        "gates": list(unique_gates.values()),
        "review_batch": review_batch,
        "review_metrics": review_metrics,
        "run_mappings": run_mappings,
        "audit": audit,
        "payload_path": None,
        "output_path": None,
        "verification": None,
    }
    atomic_json(paths.current_run, state)
    atomic_json(run_dir / "manifest.json", manifest)
    if unique_gates or pending_reviews:
        if unique_gates:
            atomic_json(run_dir / "human-gates.json", list(unique_gates.values()))
        if pending_reviews:
            atomic_json(run_dir / "review-batch.json", review_batch)
        return state
    if payload is None or audit is None:
        raise DailyRoiError("Preflight did not produce a writable payload")

    payload_path = run_dir / "write-payload.json"
    atomic_json(payload_path, payload)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"每日综合投产登记_{target_date.replace('-', '')}_已填写.xlsx"
    template_path = Path(templates[0]["source_path"])
    before_layout = xlsx_layout_snapshot(template_path)
    bridge.write(template_path, payload_path, output_path)
    render_dir = run_dir / "rendered"
    verification = bridge.verify(template_path, output_path, payload_path, render_dir)
    after_layout = xlsx_layout_snapshot(output_path)
    allowed_styles: dict[str, set[str]] = defaultdict(set)
    report_sheet_name = templates[0]["report"]["sheet"]
    for item in templates[0]["report"]["products"]:
        allowed_styles[report_sheet_name].update({item["cost_cell"], item["sales_cell"], item["roi_cell"]})
    allowed_styles[report_sheet_name].update({
        templates[0]["report"]["date_cell"], templates[0]["report"]["total_cost_cell"],
        templates[0]["report"]["total_sales_cell"], templates[0]["report"]["total_roi_cell"],
    })
    if templates[0].get("sku"):
        sku_sheet_name = templates[0]["sku"]["sheet"]
        for item in templates[0]["sku"]["products"]:
            allowed_styles[sku_sheet_name].update(cell for cell in (item.get("gross_cell"), item.get("brush_cell"), item.get("real_cell")) if cell)
        secondary = templates[0]["sku"].get("secondary_value_range")
        match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", secondary or "")
        if match and match.group(1) == match.group(3):
            allowed_styles[sku_sheet_name].update(f"{match.group(1)}{row}" for row in range(int(match.group(2)), int(match.group(4)) + 1))
    layout_failures = compare_layout(before_layout, after_layout, allowed_styles)
    verification["layout_failures"] = layout_failures
    if layout_failures:
        verification["status"] = "FAIL"
        verification["failures"].extend(f"layout:{item}" for item in layout_failures)
    verification["output_sha256"] = sha256_file(output_path)
    atomic_json(run_dir / "verification.json", verification)
    state.update({
        "status": "COMPLETE" if verification["status"] == "PASS" else "VERIFICATION_FAILED",
        "stage": "COMPLETE" if verification["status"] == "PASS" else "VERIFY",
        "updated_at": now_iso(),
        "payload_path": str(payload_path),
        "output_path": str(output_path),
        "verification": verification,
    })
    atomic_json(paths.current_run, state)
    atomic_json(run_dir / "run-summary.json", state)
    return state


def resolve_gate(
    workspace: Path,
    gate_id: str,
    *,
    target: str | None,
    persistence: str,
    node: str | None = None,
    node_modules: str | None = None,
) -> dict[str, Any]:
    paths = RuntimePaths.for_workspace(workspace)
    if not paths.current_run.exists():
        raise DailyRoiError("No current run exists")
    state = json.loads(paths.current_run.read_text(encoding="utf-8"))
    gate = next((item for item in state.get("gates", []) if item.get("gate_id") == gate_id), None)
    if not gate:
        raise DailyRoiError(f"Gate not found: {gate_id}")
    if persistence not in {"PERSISTENT_REUSABLE", "RUN_ONLY", "REJECTED"}:
        raise DailyRoiError(f"Invalid persistence classification: {persistence}")
    if persistence == "REJECTED":
        append_jsonl(paths.confirmations, {"gate_id": gate_id, "classification": persistence, "at": now_iso(), "response": target})
        state["status"] = "HUMAN_REQUIRED"
        atomic_json(paths.current_run, state)
        return state
    candidate = dict(gate.get("candidate_resolution") or {})
    if target:
        candidate["target"] = target
    if "entity_type" in candidate:
        if not candidate.get("target"):
            raise DailyRoiError("Mapping resolution requires a target")
        sources = [str(item) for item in (candidate.get("sources") or [candidate.get("source")]) if str(item or "").strip()]
        if not sources:
            raise DailyRoiError("Mapping resolution requires at least one source")
        for source in sources:
            mapping_key = f"{candidate['entity_type']}:{norm(source)}"
            state.setdefault("run_mappings", {})[mapping_key] = candidate["target"]
            if persistence == "PERSISTENT_REUSABLE":
                LocalMemory(paths).add_mapping(candidate["entity_type"], source, candidate["target"], gate_id=gate_id)
    elif candidate.get("rule"):
        state.setdefault("run_mappings", {})[f"workflow:{candidate['rule']}"] = "confirmed"
        if persistence == "PERSISTENT_REUSABLE":
            LocalMemory(paths).add_rule(candidate["rule"], gate_id=gate_id)
    else:
        raise DailyRoiError("This gate has no structured resolution candidate; provide a supported mapping or rule")
    append_jsonl(paths.confirmations, {"gate_id": gate_id, "classification": persistence, "at": now_iso(), "resolution": candidate})
    state["gates"] = [item for item in state.get("gates", []) if item.get("gate_id") != gate_id]
    state["updated_at"] = now_iso()
    atomic_json(paths.current_run, state)
    return run_report(
        workspace,
        Path(state["input_dir"]),
        Path(state["output_dir"]),
        node=node,
        node_modules=node_modules,
        existing_state=state,
    )


def resolve_review_batch(
    workspace: Path,
    responses: list[dict[str, Any]] | None = None,
    *,
    reply_text: str | None = None,
    accept_all: bool = False,
    default_persistence: str = "RUN_ONLY",
    node: str | None = None,
    node_modules: str | None = None,
) -> dict[str, Any]:
    """Apply one complete business review batch, persist once, and resume once."""
    paths = RuntimePaths.for_workspace(workspace)
    if not paths.current_run.exists():
        raise DailyRoiError("No current run exists")
    state = json.loads(paths.current_run.read_text(encoding="utf-8"))
    batch = dict(state.get("review_batch") or {})
    pending = {str(item.get("review_id")): item for item in batch.get("items", []) if item.get("status") == "PENDING"}
    if not pending:
        raise DailyRoiError("No pending inferred-review batch exists")
    if default_persistence not in {"PERSISTENT_REUSABLE", "RUN_ONLY"}:
        raise DailyRoiError(f"Invalid review persistence classification: {default_persistence}")
    human_responses: list[dict[str, Any]] = []
    if reply_text is not None:
        try:
            parsed_reply = parse_review_reply(reply_text, batch, human_gates=list(state.get("gates") or []))
            normalized = parsed_reply["responses"]
            human_responses = list(parsed_reply.get("human_responses") or [])
        except ValueError as exc:
            raise DailyRoiError(str(exc)) from exc
    elif accept_all:
        normalized = [
            {"review_id": review_id, "action": "ACCEPT", "persistence": default_persistence}
            for review_id in pending
        ]
    else:
        normalized = list(responses or [])
    response_ids = [str(item.get("review_id") or "") for item in normalized]
    if len(response_ids) != len(set(response_ids)):
        raise DailyRoiError("Each review_id may appear only once")
    missing = sorted(set(pending) - set(response_ids))
    unknown = sorted(set(response_ids) - set(pending))
    if missing or unknown:
        raise DailyRoiError(f"Review batch must be resolved together; missing={missing}, unknown={unknown}")

    memory = LocalMemory(paths)
    planned_mappings: dict[tuple[str, str], str] = {}

    def validate_target(item: dict[str, Any], target: str) -> None:
        resolution = dict(item.get("resolution_candidate") or {})
        template_model = state.get("template_model") or {}
        target_kind = str(resolution.get("target_kind") or "product")
        if target_kind == "store":
            allowed_targets = [str(group.get("store") or "") for group in template_model.get("store_groups", [])]
        else:
            allowed_targets = [str(product.get("name") or "") for product in (template_model.get("report") or {}).get("products", [])]
        if allowed_targets and norm(target) not in {norm(value) for value in allowed_targets}:
            raise DailyRoiError(f"Review target is not present in the current TemplateModel: {target}")

    # Validate the complete batch before mutating run state or durable memory.
    for response in normalized:
        review_id = str(response["review_id"])
        item = pending[review_id]
        action = str(response.get("action") or "").upper()
        persistence = str(response.get("persistence") or default_persistence)
        if action not in {"ACCEPT", "CORRECT", "REJECT"}:
            raise DailyRoiError(f"Unsupported review action: {action}")
        if persistence not in {"PERSISTENT_REUSABLE", "RUN_ONLY", "ELIGIBLE_ONLY"}:
            raise DailyRoiError(f"Invalid review persistence classification: {persistence}")
        resolution = dict(item.get("resolution_candidate") or {})
        members = list(resolution.get("members") or [resolution])
        target = str(item.get("proposed_answer") or "") if action == "ACCEPT" else str(response.get("target") or "").strip()
        if action == "CORRECT" and not target:
            raise DailyRoiError("CORRECT requires a non-empty target")
        if action in {"ACCEPT", "CORRECT"}:
            if not members or any(
                not str(member.get("entity_type") or "")
                or not [str(value) for value in (member.get("mapping_sources") or member.get("sources") or []) if str(value or "").strip()]
                for member in members
            ):
                raise DailyRoiError("This review item has no structured resolution target")
            validate_target(item, target)
        if persistence == "PERSISTENT_REUSABLE" and action in {"ACCEPT", "CORRECT"}:
            legacy_durable = item.get("persistence_candidate") or {}
            durable_members = list(item.get("persistence_candidates") or ([legacy_durable] if legacy_durable.get("entity_type") else []))
            if not durable_members:
                raise DailyRoiError("This inferred allocation is run-specific and is not eligible for durable memory")
            for durable in durable_members:
                entity_type = str(durable.get("entity_type") or "")
                for source in durable.get("sources", []):
                    key = (entity_type, norm(source))
                    existing = memory.resolve(entity_type, source, scope=durable.get("scope"))
                    planned = planned_mappings.get(key)
                    correction_supersedes = action == "CORRECT" and existing and norm(existing) != norm(target)
                    if ((existing and norm(existing) != norm(target) and not correction_supersedes) or (planned and norm(planned) != norm(target))):
                        raise DailyRoiError(f"Conflicting confirmed mapping for {source!r}")
                    planned_mappings[key] = target

    pending_gates = {str(gate.get("gate_id")): gate for gate in state.get("gates", [])}
    for response in human_responses:
        gate_id = str(response.get("gate_id") or "")
        gate = pending_gates.get(gate_id)
        if not gate:
            raise DailyRoiError(f"Human Gate not found: {gate_id}")
        if response.get("action") not in {"CONFIRM_HINT", "CORRECT"}:
            raise DailyRoiError(f"Unsupported Human Gate response: {response.get('action')}")
        target = str(response.get("target") or "").strip()
        candidate = dict(gate.get("candidate_resolution") or {})
        sources = [str(value) for value in candidate.get("sources", []) if str(value or "").strip()]
        if not target or not candidate.get("entity_type") or not sources:
            raise DailyRoiError("Human Gate reply has no structured mapping target")
        allowed_stores = [str(group.get("store") or "") for group in (state.get("template_model") or {}).get("store_groups", [])]
        if allowed_stores and norm(target) not in {norm(value) for value in allowed_stores}:
            raise DailyRoiError(f"Human Gate target is not present in the current TemplateModel: {target}")

    metrics = dict(state.get("review_metrics") or {})
    metrics.setdefault("review_accept_count", 0)
    metrics.setdefault("review_reject_count", 0)
    metrics.setdefault("review_correct_count", 0)
    metrics.setdefault("memory_candidates", sum(bool(item.get("persistence_candidate")) for item in pending.values()))
    metrics.setdefault("durable_memory_eligible", sum(bool(item.get("persistence_candidate")) for item in pending.values()))
    metrics.setdefault("run_only_not_persisted", 0)
    metrics.setdefault("review_accept_persisted", 0)
    metrics.setdefault("human_correction_persisted", 0)
    metrics.setdefault("rejected_proposals_persisted_as_fact", 0)
    for response in normalized:
        review_id = str(response["review_id"])
        item = pending[review_id]
        action = str(response.get("action") or "").upper()
        persistence = str(response.get("persistence") or default_persistence)
        if persistence not in {"PERSISTENT_REUSABLE", "RUN_ONLY", "ELIGIBLE_ONLY"}:
            raise DailyRoiError(f"Invalid review persistence classification: {persistence}")
        proposal = str(item.get("proposed_answer") or "")
        resolution = dict(item.get("resolution_candidate") or {})
        members = list(resolution.get("members") or [resolution])
        mapping_sources = list(dict.fromkeys(
            str(value)
            for member in members
            for value in (member.get("mapping_sources") or member.get("sources") or [])
            if str(value or "").strip()
        ))
        confirmation = {
            "review_id": review_id,
            "batch_id": batch.get("batch_id"),
            "decision_type": "HUMAN_CONFIRMED" if action in {"ACCEPT", "CORRECT"} else "REJECTED",
            "action": action,
            "proposal": proposal,
            "at": now_iso(),
        }
        legacy_durable = item.get("persistence_candidate") or {}
        durable_members = list(item.get("persistence_candidates") or ([legacy_durable] if legacy_durable.get("entity_type") else []))
        remember_requested = persistence in {"PERSISTENT_REUSABLE", "ELIGIBLE_ONLY"}
        persist_mapping = bool(remember_requested and durable_members)
        if remember_requested:
            metrics["run_only_not_persisted"] += max(0, len(members) - len(durable_members))
        if action == "ACCEPT":
            for key in item.get("member_keys", []):
                state.setdefault("run_mappings", {})[str(key)] = "accepted"
            for member in members:
                entity_type = str(member.get("entity_type") or "")
                for source in member.get("mapping_sources") or member.get("sources") or []:
                    state.setdefault("run_mappings", {})[f"{entity_type}:{norm(source)}"] = proposal
            if persist_mapping:
                for durable in durable_members:
                    for source in durable.get("sources", []):
                        memory.add_mapping(
                            str(durable["entity_type"]), str(source), proposal, gate_id=review_id,
                            memory_type=durable.get("memory_type"), scope=durable.get("scope"),
                            confirmation_mode="REVIEW_ACCEPT", original_proposal=proposal,
                            evidence_at_confirmation=item.get("evidence_summary", []), source_run=state.get("run_id"),
                        )
                metrics["review_accept_persisted"] += 1
            metrics["review_accept_count"] += 1
            confirmation.update(
                final_answer=proposal,
                classification="PERSISTENT_REUSABLE" if persist_mapping else "RUN_ONLY",
                rejected=False,
            )
        elif action == "CORRECT":
            target = str(response.get("target") or "").strip()
            if not target:
                raise DailyRoiError("CORRECT requires a non-empty target")
            if not members or not mapping_sources:
                raise DailyRoiError("This review item has no structured correction target")
            for member in members:
                entity_type = str(member.get("entity_type") or "")
                for source in member.get("mapping_sources") or member.get("sources") or []:
                    state.setdefault("run_mappings", {})[f"{entity_type}:{norm(source)}"] = target
            if persist_mapping:
                for durable in durable_members:
                    for source in durable.get("sources", []):
                        memory.add_mapping(
                            str(durable["entity_type"]), str(source), target, gate_id=review_id,
                            memory_type=durable.get("memory_type"), scope=durable.get("scope"),
                            confirmation_mode="HUMAN_CORRECTION", original_proposal=proposal,
                            evidence_at_confirmation=item.get("evidence_summary", []), source_run=state.get("run_id"),
                            supersede=True,
                        )
            if persist_mapping:
                metrics["human_correction_persisted"] += 1
            metrics["review_correct_count"] += 1
            confirmation.update(
                final_answer=target,
                classification="PERSISTENT_REUSABLE" if persist_mapping else "RUN_ONLY",
                rejected=True,
            )
        elif action == "REJECT":
            for key in item.get("member_keys", []):
                state.setdefault("run_mappings", {})[str(key)] = "rejected"
            metrics["review_reject_count"] += 1
            memory.record_rejected_proposal(
                decision_id=review_id,
                sources=mapping_sources or item.get("sources", []),
                proposal=proposal,
                source_run=state.get("run_id"),
            )
            confirmation.update(final_answer=None, classification="RUN_ONLY", rejected=True)
        else:
            raise DailyRoiError(f"Unsupported review action: {action}")
        append_jsonl(paths.confirmations, confirmation)

    resolved_gate_ids = set()
    for response in human_responses:
        gate_id = str(response["gate_id"])
        gate = pending_gates[gate_id]
        candidate = dict(gate.get("candidate_resolution") or {})
        target = str(response["target"])
        entity_type = str(candidate["entity_type"])
        sources = [str(value) for value in candidate.get("sources", []) if str(value or "").strip()]
        for source in sources:
            state.setdefault("run_mappings", {})[f"{entity_type}:{norm(source)}"] = target
        append_jsonl(paths.confirmations, {
            "gate_id": gate_id,
            "decision_type": "HUMAN_CONFIRMED",
            "classification": "RUN_ONLY",
            "action": response.get("action"),
            "evidence_status": response.get("evidence_status"),
            "at": now_iso(),
            "resolution": {**candidate, "target": target},
        })
        resolved_gate_ids.add(gate_id)

    state["review_metrics"] = metrics
    state["review_batch"] = None
    if resolved_gate_ids:
        state["gates"] = [gate for gate in state.get("gates", []) if str(gate.get("gate_id")) not in resolved_gate_ids]
    state["updated_at"] = now_iso()
    atomic_json(paths.current_run, state)
    return run_report(
        workspace,
        Path(state["input_dir"]),
        Path(state["output_dir"]),
        node=node,
        node_modules=node_modules,
        existing_state=state,
    )


def reset_memory(workspace: Path, *, include_audit: bool = False) -> list[str]:
    paths = RuntimePaths.for_workspace(workspace)
    removed = []
    for target in [paths.memory, paths.current_run]:
        if target.exists():
            target.unlink()
            removed.append(str(target))
    if include_audit:
        if paths.confirmations.exists():
            paths.confirmations.unlink()
            removed.append(str(paths.confirmations))
        if paths.runs_dir.exists():
            shutil.rmtree(paths.runs_dir)
            removed.append(str(paths.runs_dir))
    return removed
