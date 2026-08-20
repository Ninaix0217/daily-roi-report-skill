# Architecture

## Boundary

The system is intentionally a local-file Codex Skill. It does not replace the business process; it separates the validated Baseline into an orchestration layer and deterministic operations.

```text
Codex + SKILL.md
  -> Python workflow engine
       -> content-based input classification
       -> Decimal accounting and reconciliation
       -> workspace Local Memory / Human Gates / run state
       -> legacy .xls conversion
       -> artifact-tool workbook worker
            -> dynamic TemplateModel
            -> protected write
            -> reopen/verify/render
```

## Components

- `SKILL.md`: concise control plane and safety contract.
- `scripts/daily_roi.py`: CLI.
- `scripts/daily_roi_lib.py`: discovery, memory, gates, accounting, state, conversion, and orchestration.
- `scripts/workbook_worker.mjs`: workbook inspection, dynamic model construction, writing, formula validation, and rendering.
- `schemas/`: persisted data contracts.
- `references/`: phase-specific rules loaded progressively.
- `tests/` and `evals/`: unit, integration, Human Gate, and private Golden harnesses.

## Runtime boundaries

Skill source is developer-managed and replaceable. `.daily-roi/` is employee-owned and workspace-scoped. Run state is daily/audit state; it is not promoted to durable memory. Golden fixtures are test-only and excluded from distributable business knowledge.

## TemplateModel

The worker locates report and support structures from semantic headers, formulas, and cell relationships. It records sheets, report products, dates, writable cells, SKU mappings, product/store groups, and layout properties. Production code does not use the Golden case's observed product count, SKU count, sheet names, ranges, or stores as constants.

## Technology choice

Python is the orchestration language because its standard library provides reliable `Decimal`, filesystem hashing, XML/ZIP inspection, process control, and test isolation. LibreOffice supplies repeatable headless legacy `.xls` conversion. A small JavaScript worker uses the Codex-bundled `@oai/artifact-tool`, which is the supported spreadsheet artifact path and preserves workbook structure better than reconstructing the file from tabular data. This split is narrower than a framework and keeps every accounting decision testable.
