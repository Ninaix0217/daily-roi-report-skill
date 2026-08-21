# Changelog

## Unreleased

- Added Evidence Resolution v1.2 search-exhaustion audit, positive-evidence safety for short Chinese stems, supported-candidate preference, deterministic stale-template date updates, and independent business decision consolidation.
- Added Review UX v2 with prefilled risk-ordered decisions, copyable recommended replies, natural-language batch parsing, and run-only default acceptance.
- Added Memory Layer v2 eligibility, minimum-safe scope, typed/provenanced facts, rejected-proposal audit, hard-identity conflict handling, lifecycle transitions, and lineage deduplication.
- Added simulated, provenance-marked failure-archetype scenarios plus runner-level pending-review write-boundary regression. These fixtures do not reproduce an original external run.
- Added a minimal development-validation dependency manifest and full runtime-instance JSON Schema validator.

## 0.1.0-rc.4 — 2026-08-20

- Added evidence classes and the `VERIFIED` / `INFERRED_REVIEW` / `HUMAN_REQUIRED` decision model.
- Added one-shot batch Review with explicit accept, reject, and correct transitions before workbook writing.
- Upgraded accepted/corrected reusable mappings to human-confirmed Local Memory while keeping run-specific allocations local to the run.
- Added review metrics, provenance schemas, and sanitized RC3 behavior replay coverage.

## 0.1.0-rc.3 — 2026-08-20

- Added unique global cent-constraint resolution across current-run campaign files and ledger store totals.
- Unified SKU, product ID, placement ID, and platform item ID lookup through a product-identity index.
- Bounded semantic candidate generation to the strongest available current-run source scope and exposed candidate evidence, scope, and reason.
- Preserved ambiguous, conflicting, weak, or unreconciled cases as Human Gates.

## 0.1.0-rc.2 — 2026-08-20

- Added Evidence Resolution Layer v1 between unknown detection and Human Gate creation.
- Added exact SKU/platform-item identity resolution, structural generic-plan attribution, bounded semantic/context resolution, contradiction checks, and auditable decision classes.
- Coalesced alias-family blockers into one business question and allowed one confirmation to resolve every grouped source.
- Preserved `v0.1.0-rc.1` as the frozen external acceptance baseline.

## 0.1.0 — 2026-08-20

- Created standalone Codex Skill structure and discovery metadata.
- Added dynamic template inspection and format-preserving workbook writer.
- Added Decimal/integer-cent accounting, legacy `.xls` conversion, and deterministic reconciliation.
- Added workspace-local schema-controlled memory and structured Human Gates with pause/persist/resume.
- Added private Golden regression, Human Gate A–H, unit, integration, and isolation verification.
- Added release dependency preflight and clean-room installation guidance.
