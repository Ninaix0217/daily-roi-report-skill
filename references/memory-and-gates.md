# Local memory and Human Gates

## Lifecycles

| Data | Owner | Location | Lifetime |
|---|---|---|---|
| Skill source | Developer | installed skill directory | replaced by skill upgrades |
| Local memory | Employee workspace | `<workspace>/.daily-roi/memory.json` | survives skill upgrades |
| Confirmation audit | Employee workspace | `<workspace>/.daily-roi/confirmations.jsonl` | append-only until explicit reset |
| Current/run state | Employee workspace | `<workspace>/.daily-roi/current-run.json` and `runs/` | task/audit state, never default knowledge |
| Golden fixtures | Developer | `evals/` | test only |

Memory v1 accepts confirmed entity mappings for `store`, `product`, `campaign`, and `sku`, plus the single controlled rule `auto_update_stale_template_date`. It rejects candidate status, unreviewed inference, free-form rules, and conflicting mappings. An accepted or corrected Review becomes `HUMAN_CONFIRMED`; only an explicitly reusable, schema-eligible mapping may then enter durable memory. Run-specific global allocations never become durable mappings.

## Gate policy

- `HG-01`: unknown nonzero entity mapping.
- `HG-02`: business-source date conflict or unconfirmed stale-template policy.
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

All pending Review items must be answered before one resume. Missing answers never imply acceptance.

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

A grouped mapping contains all equivalent sources. One confirmation applies to every listed source; persistence writes each exact source mapping separately so later exact lookup remains deterministic.

The resume operation retains the run ID and run mappings, rereads inputs, and recomputes deterministic checks. This avoids restarting conversational analysis while detecting source mutations.
