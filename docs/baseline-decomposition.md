# Baseline decomposition

`BASELINE_WORKFLOW_V1` is preserved unchanged as local development evidence at `development-evidence/baseline-workflow-v1.md`. That file is intentionally excluded from the distributable Git set because it contains one employee's confirmed aliases and examples.

The v0 decomposition preserves behavior rather than shortening policy:

| Baseline concern | v0 location |
|---|---|
| Execution order and stop-before-write | `SKILL.md`, `references/workflow.md`, workflow engine |
| Template is authoritative | workbook worker and `references/template-and-workbook.md` |
| File identification and dates | Python discovery/preflight |
| Mapping decisions | Evidence Resolution Layer, followed by structured Human Gates only for unresolved business facts |
| Confirmed aliases/rules | employee `.daily-roi/memory.json`, never Skill Core |
| Deduplication | deterministic identity classifier and audit |
| Product-level cost aggregation | accounting payload builder |
| Store/global reconciliation | deterministic gates and audit |
| SKU sales and brushing | template-derived SKU model and Decimal sums |
| Formula/layout preservation | worker write and OOXML verification |
| Delivery summary | output contract |

The Golden case's observed facts are present only in expected fixtures and test memory. They are not production assumptions.

`v0.1.0-rc.1` remains the frozen distribution baseline. The rc.2 architecture correction does not relax accounting, deduplication, write, or reconciliation invariants; it corrects the earlier orchestration assumption that a Local Memory miss necessarily requires a human question.

## Optimization candidates

No Baseline rule was deleted in v0. Potential future wording consolidation belongs in a Baseline-versus-Variant experiment after behavioral equivalence is retained by the full eval suite.
