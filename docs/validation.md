# Validation

## Layers

- Input: file manifest, SHA-256, content classification, internal dates, required sources, exact duplicate files.
- Template: dynamic products/SKUs/store groups, sheets, writable regions, formula precedent, structure fingerprint.
- Expense: product component totals, each multi-product store, financial ledger equality, duplicate decisions.
- Evidence resolution and review: unified exact product identity, structural generic plans, bounded candidate generation, unique global store constraints, auditable evidence classes, batched inferred review, ambiguous Human decisions, contradiction handling, and accepted/corrected learning transitions.
- Sales: text SKU coverage, external SKU gates, reported total equality, explicit brushing subtraction.
- Workbook: formulas, expected values, error tokens, sheet/order/product structure, merges, widths, heights, critical styles.
- Visual: every sheet is rendered; PASS is assigned only after images are reviewed.

## Private Golden regression

The private Golden harness reads the locally retained original files, including the legacy financial `.xls`, uses isolated test memory for employee-specific confirmations, produces a new workbook, and compares it with the known-good artifact. Expected business totals and private fixtures remain ignored local evidence and are not part of the release candidate.

The comparison checks target date, paid cost, gross sales, brushing, real sales, every runtime multi-store reconciliation, formula map, and layout fingerprint. It is not a totals-only test.

Tracked review-layer fixtures are simulated archetype scenarios. Their historical basis is limited to observed failure shapes; they are not retained external-run replays and cannot establish `REAL VERIFIED` status.

## Commands

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/validate_schemas.py
python <LOCAL_PRIVATE_GOLDEN_HARNESS> --source <PRIVATE_GOLDEN_INPUT_DIR> --known-good <KNOWN_GOOD_XLSX> --node <NODE> --node-modules <NODE_MODULES>
python <SKILL_CREATOR>/scripts/quick_validate.py .
node --check scripts/workbook_worker.mjs
```

## Verification labels

- `REAL VERIFIED`: executed locally against retained private source inputs and a known-good output.
- `FIXTURE VERIFIED`: executed with constructed workspaces/records that isolate a behavior.
- `UNIT VERIFIED`: deterministic function behavior executed in unit tests.
- `DOC VERIFIED`: checked against current official documentation but not an end-to-end runtime claim.
- `UNVERIFIED`: explicitly not executed or not reviewable; never promoted to PASS by implication.
