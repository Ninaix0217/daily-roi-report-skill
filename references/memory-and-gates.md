# Local memory and Human Gates

## Lifecycles

| Data | Owner | Location | Lifetime |
|---|---|---|---|
| Skill source | Developer | installed skill directory | replaced by skill upgrades |
| Local memory | Employee workspace | `<workspace>/.daily-roi/memory.json` | survives skill upgrades |
| Confirmation audit | Employee workspace | `<workspace>/.daily-roi/confirmations.jsonl` | append-only until explicit reset |
| Current/run state | Employee workspace | `<workspace>/.daily-roi/current-run.json` and `runs/` | task/audit state, never default knowledge |
| Golden fixtures | Developer | `evals/` | test only |

Memory v1 accepts confirmed entity mappings for `store`, `product`, `campaign`, and `sku`, plus the single controlled rule `auto_update_stale_template_date`. It rejects candidate status, AI inference, free-form rules, and conflicting mappings.

## Gate policy

- `HG-01`: unknown nonzero entity mapping.
- `HG-02`: business-source date conflict or unconfirmed stale-template policy.
- `HG-03`: suspected duplicate without sufficient identity proof.
- `HG-04`: expense/store/sales reconciliation failure.
- `HG-05`: template-external nonzero SKU or incomplete template SKU coverage.
- `HG-06`: unknown nonzero campaign/product attribution or ambiguous input classification.

Each gate stores an ID, type, blocking reason, evidence, optional candidate resolution, question, and optional persistence candidate. The assistant must show all safely discoverable gates together.

## Resolution semantics

- `PERSISTENT_REUSABLE`: only when the user explicitly confirms a reusable mapping or controlled rule. Write memory and resume.
- `RUN_ONLY`: apply to `current-run.json` only, append audit, and resume.
- `REJECTED`: append the rejection; do not mutate memory and do not bypass the blocker.

The resume operation retains the run ID and run mappings, rereads inputs, and recomputes deterministic checks. This avoids restarting conversational analysis while detecting source mutations.
