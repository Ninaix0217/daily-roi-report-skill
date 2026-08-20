#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from daily_roi_lib import DailyRoiError, LocalMemory, RuntimePaths, dependency_preflight, reset_memory, resolve_gate, run_report


def summary(state: dict) -> dict:
    audit = state.get("audit") or {}
    sales = audit.get("sales") or {}
    verification = state.get("verification") or {}
    return {
        "status": state.get("status"),
        "stage": state.get("stage"),
        "run_id": state.get("run_id"),
        "target_date": state.get("target_date"),
        "input_file_count": len(state.get("manifest", [])),
        "human_gate_count": len(state.get("gates", [])),
        "gates": state.get("gates", []),
        "financial_total": _money(audit.get("financial_total_cents")),
        "product_expense_total": _money(audit.get("product_expense_total_cents")),
        "expense_difference": _money(audit.get("expense_difference_cents")),
        "sales_reported": _money(sales.get("reported_cents")),
        "sales_sku_sum": _money(sales.get("sku_sum_cents")),
        "output_path": state.get("output_path"),
        "verification_status": verification.get("status"),
        "visual_verification_level": verification.get("visual_verification_level"),
    }


def _money(cents):
    if cents is None:
        return None
    return f"{Decimal(cents) / Decimal(100):.2f}"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Deterministic daily ROI report workflow")
    sub = root.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Discover, preflight, reconcile, write, and verify")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--input-dir", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--node")
    run.add_argument("--node-modules")

    preflight = sub.add_parser("preflight", help="Check local dependencies before processing business files")
    preflight.add_argument("--input-dir", type=Path, required=True)
    preflight.add_argument("--node")
    preflight.add_argument("--node-modules")

    resolve = sub.add_parser("resolve", help="Resolve one structured Human Gate and resume")
    resolve.add_argument("--workspace", type=Path, default=Path.cwd())
    resolve.add_argument("--gate-id", required=True)
    resolve.add_argument("--target")
    resolve.add_argument("--persistence", choices=["PERSISTENT_REUSABLE", "RUN_ONLY", "REJECTED"], required=True)
    resolve.add_argument("--node")
    resolve.add_argument("--node-modules")

    status = sub.add_parser("status", help="Show the concise current-run status")
    status.add_argument("--workspace", type=Path, default=Path.cwd())

    memory = sub.add_parser("memory", help="Show schema-controlled local memory")
    memory.add_argument("--workspace", type=Path, default=Path.cwd())

    reset = sub.add_parser("reset-memory", help="Delete this workspace's local memory")
    reset.add_argument("--workspace", type=Path, default=Path.cwd())
    reset.add_argument("--include-audit", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "preflight":
            result = dependency_preflight(args.input_dir, node=args.node, node_modules=args.node_modules)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            state = run_report(args.workspace, args.input_dir, args.output_dir, node=args.node, node_modules=args.node_modules)
            print(json.dumps(summary(state), ensure_ascii=False, indent=2))
            return 2 if state["status"] == "HUMAN_REQUIRED" else (0 if state["status"] == "COMPLETE" else 1)
        if args.command == "resolve":
            state = resolve_gate(args.workspace, args.gate_id, target=args.target, persistence=args.persistence, node=args.node, node_modules=args.node_modules)
            print(json.dumps(summary(state), ensure_ascii=False, indent=2))
            return 2 if state["status"] == "HUMAN_REQUIRED" else (0 if state["status"] == "COMPLETE" else 1)
        if args.command == "status":
            current = RuntimePaths.for_workspace(args.workspace).current_run
            if not current.exists():
                raise DailyRoiError("No current run exists")
            print(json.dumps(summary(json.loads(current.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2))
            return 0
        if args.command == "memory":
            print(json.dumps(LocalMemory(RuntimePaths.for_workspace(args.workspace)).data, ensure_ascii=False, indent=2))
            return 0
        if args.command == "reset-memory":
            removed = reset_memory(args.workspace, include_audit=args.include_audit)
            print(json.dumps({"removed": removed}, ensure_ascii=False, indent=2))
            return 0
    except DailyRoiError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
