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


MONEY = Decimal("0.01")
STATE_DIR_NAME = ".daily-roi"
MEMORY_FILE = "memory.json"
CONFIRMATIONS_FILE = "confirmations.jsonl"
CURRENT_RUN_FILE = "current-run.json"
SUPPORTED_MAPPING_TYPES = {"store", "product", "campaign", "sku"}
WORKFLOW_RULE_STALE_TEMPLATE = "auto_update_stale_template_date"
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
            return {"schema_version": 1, "entity_mappings": [], "workflow_rules": []}
        data = json.loads(self.paths.memory.read_text(encoding="utf-8"))
        self.validate(data)
        return data

    @staticmethod
    def validate(data: dict[str, Any]) -> None:
        if data.get("schema_version") != 1:
            raise DailyRoiError("Unsupported local memory schema_version")
        if not isinstance(data.get("entity_mappings"), list) or not isinstance(data.get("workflow_rules"), list):
            raise DailyRoiError("Invalid local memory collections")
        for item in data["entity_mappings"]:
            if item.get("kind") != "entity_mapping" or item.get("entity_type") not in SUPPORTED_MAPPING_TYPES:
                raise DailyRoiError(f"Invalid entity mapping: {item}")
            if item.get("status") != "confirmed" or item.get("source_type") != "human_confirmation":
                raise DailyRoiError("Only human-confirmed mappings may be durable")
            if not str(item.get("source", "")).strip() or not str(item.get("target", "")).strip():
                raise DailyRoiError("Mapping source and target are required")
        for item in data["workflow_rules"]:
            if item.get("kind") != "workflow_rule" or item.get("status") != "confirmed":
                raise DailyRoiError(f"Invalid workflow rule: {item}")
            if item.get("rule") != WORKFLOW_RULE_STALE_TEMPLATE:
                raise DailyRoiError(f"Unsupported workflow rule: {item.get('rule')}")

    def save(self) -> None:
        self.validate(self.data)
        atomic_json(self.paths.memory, self.data)

    def resolve(self, entity_type: str, source: str) -> str | None:
        wanted = norm(source)
        for item in reversed(self.data["entity_mappings"]):
            if item["entity_type"] == entity_type and norm(item["source"]) == wanted:
                return str(item["target"])
        return None

    def has_rule(self, rule: str) -> bool:
        return any(item.get("rule") == rule and item.get("status") == "confirmed" for item in self.data["workflow_rules"])

    def add_mapping(self, entity_type: str, source: str, target: str, *, gate_id: str) -> None:
        if entity_type not in SUPPORTED_MAPPING_TYPES:
            raise DailyRoiError(f"Unsupported mapping entity_type: {entity_type}")
        current = self.resolve(entity_type, source)
        if current and norm(current) != norm(target):
            raise DailyRoiError(f"Conflicting confirmed mapping for {source!r}: {current!r} vs {target!r}")
        if current:
            return
        self.data["entity_mappings"].append({
            "kind": "entity_mapping",
            "entity_type": entity_type,
            "source": source,
            "target": target,
            "status": "confirmed",
            "source_type": "human_confirmation",
            "confirmed_at": now_iso(),
            "gate_id": gate_id,
        })
        self.save()

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


def filename_date_evidence(path: Path, internal_dates: set[str]) -> tuple[set[str], str]:
    if internal_dates:
        return internal_dates, "internal"
    return extract_dates_from_name(path.name), "filename_fallback_no_internal_date_column"


def parse_campaign(path: Path) -> dict[str, Any] | None:
    headers, rows, encoding = read_csv_rows(path)
    cost_header = header_index(headers, "花费", "费用", "支出")
    if not cost_header:
        return None
    id_header = header_index(headers, "ID", "计划ID", "记录ID")
    sku_header = header_index(headers, "SKU ID", "SKU")
    date_header = header_index(headers, "日期", "时间", "投放日期")
    plan_header = headers[0] if headers else None
    records = []
    internal_dates = set()
    for index, row in enumerate(rows, 2):
        if date_header:
            internal_dates |= extract_dates([row.get(date_header)])
        records.append({
            "row": index,
            "plan": row.get(plan_header, "") if plan_header else "",
            "record_id": row.get(id_header, "") if id_header else "",
            "sku": row.get(sku_header, "") if sku_header else "",
            "cost_cents": to_cents(row.get(cost_header, "0")),
            "raw": row,
        })
    dates, date_evidence = filename_date_evidence(path, internal_dates)
    return {
        "kind": "campaign_full_store" if sku_header else "campaign_regular",
        "path": str(path.resolve()),
        "name": path.name,
        "headers": headers,
        "encoding": encoding,
        "dates": sorted(dates),
        "date_evidence": date_evidence,
        "records": records,
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
    has_rule = memory.has_rule(WORKFLOW_RULE_STALE_TEMPLATE) or bool((run_mappings or {}).get(f"workflow:{WORKFLOW_RULE_STALE_TEMPLATE}"))
    if update_template and not has_rule:
        gates.append(make_gate(
            "HG-02", "Only the template date differs from a single consistent business date", {"template_date": template_date, "business_date": target_date},
            f"所有业务源均为 {target_date}，模板为 {template_date}。是否更新模板日期？",
            candidate={"rule": WORKFLOW_RULE_STALE_TEMPLATE},
            persistence={"rule": WORKFLOW_RULE_STALE_TEMPLATE},
        ))
    return target_date, update_template and has_rule, gates


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
    products = [item["name"] for item in template["report"]["products"]]
    product_keys = {norm(item): item for item in products}
    raw_groups = template.get("store_groups", [])
    group_names = [item["store"] for item in raw_groups]
    group_products: dict[str, set[str]] = {}
    for group in raw_groups:
        resolved = set()
        for member in group.get("products", []):
            product = resolve_product(member, products, memory, run_mappings)
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

    for campaign in regular:
        non_generic_products: set[str] = set()
        provisional = []
        for record in campaign["records"]:
            plan = record["plan"]
            generic = norm(plan) in {"单盒", "三盒", "1", "3", "一盒", "3盒", "1盒"}
            resolved = None if generic else resolve_product(plan, products, memory, run_mappings)
            provisional.append((record, generic, resolved))
            if resolved and record["cost_cents"]:
                non_generic_products.add(resolved)
            elif record["cost_cents"] and not generic:
                candidate = {"entity_type": "campaign", "source": plan, "target": None}
                stripped = strip_campaign_spec(plan)
                likely = [p for p in products if norm(stripped) and (norm(stripped) in norm(p) or norm(p) in norm(stripped))]
                if len(likely) == 1:
                    candidate["target"] = likely[0]
                gates.append(make_gate(
                    "HG-06", "Nonzero campaign cannot be uniquely attributed", {"file": campaign["name"], "row": record["row"], "campaign": plan, "amount": cents_text(record["cost_cents"])},
                    f"非零计划“{plan}”（{cents_text(record['cost_cents'])}）应归属哪个模板产品？",
                    candidate=candidate,
                    persistence=candidate,
                ))
        if campaign["total_cents"] == 0:
            # Zero-only detail cannot affect accounting and often contains a
            # broad dormant campaign catalog.  Do not infer its store.
            continue
        candidates = [store for store, members in group_products.items() if non_generic_products and non_generic_products.issubset(members)]
        if len(candidates) != 1:
            if campaign["total_cents"]:
                gates.append(make_gate(
                    "HG-06", "Campaign detail file cannot be uniquely assigned to a store", {"file": campaign["name"], "resolved_products": sorted(non_generic_products), "candidate_stores": candidates, "amount": cents_text(campaign["total_cents"])},
                    f"明细文件 {campaign['name']} 无法唯一归属店铺，请确认。",
                ))
            continue
        store = candidates[0]
        detailed_stores.add(store)
        split = defaultdict(int)
        main_product = _main_store_product(store, products, memory, run_mappings)
        for record, generic, resolved in provisional:
            if generic:
                resolved = main_product
            if not resolved:
                if record["cost_cents"]:
                    gates.append(make_gate(
                        "HG-06", "Generic nonzero campaign has no resolved store main product", {"file": campaign["name"], "row": record["row"], "campaign": record["plan"], "store": store, "amount": cents_text(record["cost_cents"])},
                        f"店铺 {store} 的计划“{record['plan']}”应归属哪个产品？",
                    ))
                continue
            split[resolved] += record["cost_cents"]
            if record["cost_cents"]:
                component = {"cents": record["cost_cents"], "source_file": campaign["name"], "row": record["row"], "kind": "campaign_regular", "store": store, "plan": record["plan"], "record_id": record["record_id"]}
                components[resolved].append(component)
                audit_campaign_records.append({**record, "resolved_product": resolved, "source_kind": campaign["kind"], "source_file": campaign["name"], "date": target_date})
        store_splits[store] = {"detail_files": [campaign["name"]], "products": dict(split), "split_total_cents": sum(split.values())}

    for campaign in full_store:
        source_token = "full_store_export"
        assigned = run_mappings.get(f"campaign:{norm(source_token)}") or memory.resolve("campaign", source_token)
        store = resolve_store(assigned or "", group_names, memory, run_mappings) if assigned else None
        if not store and campaign["total_cents"]:
            gates.append(make_gate(
                "HG-06", "Full-store export has no account field and no confirmed local attribution", {"file": campaign["name"], "amount": cents_text(campaign["total_cents"]), "schema": "full_store_sku_export"},
                "该全店推广导出属于哪个模板店铺？",
                candidate={"entity_type": "campaign", "source": source_token, "target": None},
                persistence={"entity_type": "campaign", "source": source_token, "target": None},
            ))
            continue
        split = defaultdict(int)
        for record in campaign["records"]:
            resolved = template.get("sku", {}).get("map", {}).get(record["sku"], {}).get("product")
            if not resolved:
                resolved = resolve_product(record["sku"], products, memory, run_mappings)
            if not resolved and record["cost_cents"]:
                gates.append(make_gate(
                    "HG-05", "Template-external nonzero SKU in campaign expense", {"file": campaign["name"], "row": record["row"], "sku": record["sku"], "amount": cents_text(record["cost_cents"])},
                    f"全店推广存在模板外非零 SKU {record['sku']}，请确认归属。",
                    candidate={"entity_type": "sku", "source": record["sku"], "target": None},
                    persistence={"entity_type": "sku", "source": record["sku"], "target": None},
                ))
                continue
            if not resolved:
                continue
            split[resolved] += record["cost_cents"]
            if record["cost_cents"]:
                component = {"cents": record["cost_cents"], "source_file": campaign["name"], "row": record["row"], "kind": "campaign_full_store", "store": store, "sku": record["sku"]}
                components[resolved].append(component)
                audit_campaign_records.append({**record, "resolved_product": resolved, "source_kind": campaign["kind"], "source_file": campaign["name"], "date": target_date})
        if store:
            detailed_stores.add(store)
            store_splits[store] = {"detail_files": [campaign["name"]], "products": dict(split), "split_total_cents": sum(split.values())}

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

    canonical_ledger: dict[str, tuple[str, int]] = {}
    for raw_store, amount in ledger_by_store.items():
        canonical = resolve_store(raw_store, group_names, memory, run_mappings)
        if canonical:
            canonical_ledger[canonical] = (raw_store, amount)
            continue
        suffix = re.split(r"[-—]", raw_store)[-1].strip()
        product = resolve_product(suffix, products, memory, run_mappings)
        if not product and amount:
            likely = [p for p in products if norm(suffix) and (norm(suffix) in norm(p) or norm(p) in norm(suffix))]
            candidate = {"entity_type": "product", "source": suffix, "target": likely[0] if len(likely) == 1 else None}
            gates.append(make_gate(
                "HG-01", "Nonzero ledger store cannot be uniquely mapped to a template product", {"store": raw_store, "source_product": suffix, "amount": cents_text(amount)},
                f"店铺“{raw_store}”（{cents_text(amount)}）对应哪个模板产品？",
                candidate=candidate,
                persistence=candidate,
            ))
            continue
        if product and amount:
            components[product].insert(0, {"cents": amount, "source_file": financial["source_name"], "kind": "financial_single_store", "store": raw_store})

    mapping_blocked = any(item["gate_type"] in {"HG-01", "HG-03", "HG-05", "HG-06"} for item in gates)
    reconciliations = []
    for store in group_names:
        raw_store, ledger_amount = canonical_ledger.get(store, (store, 0))
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
                confirmed_product = resolve_product(record["sku"], products, memory, run_mappings)
                if confirmed_product:
                    mapped = {"product": confirmed_product, "spec": "other"}
            if not mapped:
                if record["amount_cents"]:
                    gates.append(make_gate(
                        "HG-05", "Template-external sales SKU has nonzero amount", {"sku": record["sku"], "row": record["row"], "amount": cents_text(record["amount_cents"])},
                        f"销售文件中模板外 SKU {record['sku']} 有非零金额 {cents_text(record['amount_cents'])}，请确认归属。",
                        candidate={"entity_type": "sku", "source": record["sku"], "target": None},
                        persistence={"entity_type": "sku", "source": record["sku"], "target": None},
                    ))
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

    payload = {
        "schema_version": 1,
        "target_date": target_date,
        "update_template_date": template["report"].get("template_date") != target_date,
        "product_expenses": product_expenses,
        "expense_total_cents": product_total,
        "sales": sales_payload,
        "roi_number_format": "0.00",
    }
    audit = {
        "target_date": target_date,
        "financial_total_cents": ledger_total,
        "product_expense_total_cents": product_total,
        "expense_difference_cents": expense_difference,
        "multi_store_reconciliations": reconciliations,
        "proven_duplicates": [{"evidence": item["evidence"], "source_file": item["duplicate"]["source_file"], "row": item["duplicate"]["row"]} for item in proven],
        "suspected_duplicates": len(suspected),
        "sales": sales_audit,
    }
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

    unique_gates = {gate["gate_id"]: gate for gate in gates}
    state = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "HUMAN_REQUIRED" if unique_gates else "READY_TO_WRITE",
        "stage": "RESOLVE" if unique_gates else "RECONCILE",
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
        "run_mappings": run_mappings,
        "audit": audit,
        "payload_path": None,
        "output_path": None,
        "verification": None,
    }
    atomic_json(paths.current_run, state)
    atomic_json(run_dir / "manifest.json", manifest)
    if unique_gates:
        atomic_json(run_dir / "human-gates.json", list(unique_gates.values()))
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
        mapping_key = f"{candidate['entity_type']}:{norm(candidate['source'])}"
        state.setdefault("run_mappings", {})[mapping_key] = candidate["target"]
        if persistence == "PERSISTENT_REUSABLE":
            LocalMemory(paths).add_mapping(candidate["entity_type"], candidate["source"], candidate["target"], gate_id=gate_id)
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
