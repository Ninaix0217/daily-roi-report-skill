# Local memory and Human Gates

## Lifecycles

| Data | Owner | Location | Lifetime |
|---|---|---|---|
| Skill source | Developer | installed skill directory | replaced by skill upgrades |
| Local memory | Employee workspace | `<workspace>/.daily-roi/memory.json` | survives skill upgrades |
| Confirmation audit | Employee workspace | `<workspace>/.daily-roi/confirmations.jsonl` | append-only until explicit reset |
| Current/run state | Employee workspace | `<workspace>/.daily-roi/current-run.json` and `runs/` | task/audit state, never default knowledge |
| Golden fixtures | Developer | `evals/` | test only |

Memory v2 accepts scoped, human-confirmed `ENTITY_MAPPING`, `PLAN_PATTERN`, and `STORE_MAPPING` facts. A local `WORKFLOW_PREFERENCE` type is reserved for genuinely environment-specific behavior; the stale-template date rule is now Shared Core and is never Local Memory. Items carry `ACTIVE`, `SUPERSEDED`, `CONFLICTED`, or `RETIRED` lifecycle state plus review/correction provenance. Run-specific global allocations never become durable mappings.

## Gate policy

- `HG-01`: unknown nonzero entity mapping.
- `HG-02`: business-source date conflict or missing target date. A uniquely consistent business date auto-updates a stale template as `VERIFIED`.
- `HG-03`: suspected duplicate without sufficient identity proof.
- `HG-04`: expense/store/sales reconciliation failure.
- `HG-05`: template-external nonzero SKU or incomplete template SKU coverage.
- `HG-06`: unknown nonzero campaign/product attribution or ambiguous input classification.

Each gate stores an ID, type, blocking reason, evidence, alternatives/contradictions inside the resolution result, optional candidate resolution, question, and optional persistence candidate. Internal blockers are coalesced by independent business fact and alias family before presentation. The assistant must show all safely discoverable business questions together.

## Resolution semantics

`INFERRED_REVIEW` is separate from Human Gates. It always contains a proposed answer and is resolved as a complete batch:

- `ACCEPT`: approve the proposal; persist only when explicitly reusable and eligible.
- `CORRECT`: reject the proposal, validate the supplied target against the current TemplateModel, and persist only the corrected target when eligible.
- `REJECT`: do not persist the proposal; resume into `HUMAN_REQUIRED` for the unresolved fact.

All pending Review items must be answered before one resume. Missing answers never imply acceptance. The natural reply parser accepts “全部接受”, remember-eligible variants, numbered corrections, and mixed accept/correct wording, but always binds the response to the current displayed batch.

When one reply also resolves numbered Human Gates, every requested target is validated before any confirmation or mapping is written. Validation follows the decision's target domain rather than its source identity: `PRODUCT_ASSIGNMENT` targets must exist in `TemplateModel.report.products`, while `STORE_ASSIGNMENT` and `STORE_ALLOCATION` targets must exist in `TemplateModel.store_groups`. Placement IDs, campaigns, and filenames may be sources of a product assignment; they do not create a separate target namespace. Unknown target domains fail closed, and product/store names are never validated through a combined global label set.

Missing brushing is a run-scoped material-input Gate with an amount target, not a product/store mapping. `N是` explicitly confirms zero; `N改为金额` supplies a nonnegative Decimal/cents amount. Zero and nonzero confirmations remain `RUN_ONLY`, never enter durable Memory, and a multi-product nonzero total is rejected until product-level allocation is supplied. The full mixed batch is validated before any confirmation, Memory, state, or workbook mutation.

“全部接受” is run-only by default. Wording such as “能记的记住” requests an eligibility check rather than unconditional persistence. Stable mappings with a safely inferred scope may persist; amount-dependent/current-file allocation does not. A bare rejection creates rejected-proposal audit provenance, not a new mapping fact.

The CLI response file is deliberately small and structured:

```json
{
  "responses": [
    {"review_id": "RV-example1", "action": "ACCEPT", "persistence": "PERSISTENT_REUSABLE"},
    {"review_id": "RV-example2", "action": "CORRECT", "target": "Template Product", "persistence": "RUN_ONLY"},
    {"review_id": "RV-example3", "action": "REJECT"}
  ]
}
```

- `PERSISTENT_REUSABLE`: only when the user explicitly confirms a reusable mapping or controlled rule. Write memory and resume.
- `RUN_ONLY`: apply to `current-run.json` only, append audit, and resume.
- `REJECTED`: append the rejection; do not mutate memory and do not bypass the blocker.

A grouped mapping contains all equivalent sources. One confirmation applies to every listed source; persistence writes each exact source mapping separately so later exact lookup remains deterministic. Current hard identity outranks historical memory; a conflict is surfaced and the old item becomes `CONFLICTED` rather than silently winning. Human correction supersedes the prior active item without deleting its provenance.

When one independent Review consolidates product and campaign members, persistence eligibility is evaluated per original member. Every eligible member keeps its own entity type, minimum-safe scope, source lineage, and confirmation provenance. Run-only members are applied only to the current run and are never persisted merely because another member in the same Review is durable.

The resume operation retains the run ID and run mappings, rereads inputs, and recomputes deterministic checks. This avoids restarting conversational analysis while detecting source mutations.
