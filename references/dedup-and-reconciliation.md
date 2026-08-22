# Deduplication and reconciliation

## Identity policy

Similarity is not identity. Product, SKU, date, amount, store, or plan-name equality—alone or as a vague combination—does not prove a duplicate.

Automatic deduplication is allowed for:

1. files with identical SHA-256 hashes; or
2. rows with the same nonempty platform/transaction record ID and the same normalized complete source record.

Every automatic removal is recorded with its evidence. Conflicting or merely similar records produce `HG-03`; they are not silently removed. A plan ID by itself is not sufficient identity evidence.

## Expense invariants

- The final aggregation unit is product.
- Independent costs assigned to the same product are added, including across stores and promotion types.
- For each runtime-discovered multi-product store: `sum(product splits) == financial ledger store total` to the cent.
- Globally: `sum(product paid costs) == target-date quick-car deductions in the financial ledger` to the cent.
- Any nonzero difference is `HG-04`; no balancing adjustment is permitted.

## Sales invariants

- SKU is always text.
- Build `SKU -> Product` from the current template.
- Sum every SKU row with integer cents and reconcile it to the source-reported total.
- A template-external zero SKU may be audited and ignored; a nonzero one is `HG-05`.
- Missing template SKU coverage is `HG-05`.
- `real_sales = gross_sales - explicitly supplied brushing amount`.
- If sales are known but the template's brushing input area has no nonzero source value and the current run has no explicit Human confirmation, brushing is `UNKNOWN` and real sales are `UNRESOLVED`. Create a blocking material-input Human Gate; do not write or complete a workbook with blank/zero real-sales output.
- Human-confirmed zero is `KNOWN_ZERO`, not missing. A supplied nonzero total is accepted only for a single-product run; multi-product nonzero brushing requires product-level allocation and remains blocked.
- The current TemplateModel does not preserve enough provenance to prove that a zero-valued brushing cell represents complete source coverage. Source zero therefore remains fail-closed until a stronger input contract exists; existing nonzero source values retain `SOURCE` provenance.

## Money

Parse source values with `Decimal`, convert to integer cents at the currency boundary, and keep two decimal places. Never use binary floating-point accumulation as the accounting truth.
