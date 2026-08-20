# Workflow contract

The implementation preserves the Baseline order as seven resumable phases.

## DISCOVER

- Inventory every file with name, path, size, extension, and SHA-256.
- Classify by workbook/CSV contents and headers, not numeric filename prefixes.
- Identify exactly one template and one financial ledger; identify campaign and optional sales sources.
- Read source-internal dates and build the dynamic `TemplateModel`.

## PREFLIGHT

- Compare all business-source dates. A conflict between business sources is always a Human Gate.
- A stale template date may be auto-updated only when all business sources agree and the workspace memory contains the controlled confirmed rule.
- Find exact-file duplicates and record-level duplicate evidence.
- Resolve exact template names and durable/run-only mappings; collect all remaining independent gates.
- Persist `current-run.json` before returning `HUMAN_REQUIRED`.

## RESOLVE

- Validate a human response against the gate's structured candidate.
- Classify it as persistent reusable, run-only, or rejected.
- Append an auditable confirmation record. Persist only eligible human-confirmed reusable facts.
- Resume the same run ID. Reread inputs and rerun deterministic checks where necessary to ensure source files did not silently change.

## RECONCILE

- Aggregate final costs by product, with auditable source components.
- Reconcile each runtime-discovered multi-product store split to its ledger total.
- Reconcile all product costs to the financial ledger total.
- Reconcile sales SKU sums and template coverage; subtract only explicit brushing amounts.
- Any nonzero difference blocks writing.

## WRITE

- Create a new workbook from the original template.
- Modify only cells identified from runtime structure.
- Retain additive cost formulas, sales formulas, zero-safe ROI formulas, and formula-based totals.

## VERIFY

- Reopen the output and verify values, formulas, formula errors, sheet structure, product order, merges, dimensions, and critical styles.
- Render all sheets when the runtime supports it. Rendering alone is not visual PASS; a reviewer must inspect the images.

## COMPLETE

- Save the audit under `.daily-roi/runs/<run-id>/`.
- Return a concise outcome and the output path. Durable memory stays workspace-scoped; run artifacts do not become memory.
