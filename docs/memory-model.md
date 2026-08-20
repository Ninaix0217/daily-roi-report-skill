# Memory model

Local Memory v0 is a small schema-controlled file, not a free-form knowledge base.

## Entity mapping

Only `store`, `product`, `campaign`, and `sku` mappings are accepted. Every durable item records confirmed status, `human_confirmation` provenance, timestamp, and originating gate. A conflicting mapping fails closed.

## Workflow rule

The only v0 rule is `auto_update_stale_template_date`. Its condition and action are fixed by schema: all business sources must agree on one date, the template alone differs, and the action updates only the template date. It cannot bypass a business-source date conflict.

## Persistence decision

- `PERSISTENT_REUSABLE`: write eligible structured fact/rule to memory and apply it to the run.
- `RUN_ONLY`: write only to current run mappings and confirmation audit.
- `REJECTED`: audit the rejection and retain the blocker.

AI-generated candidates are questions, never memory. Natural-language summaries are not accepted by the memory writer.

## Isolation and reset

The state root is resolved from the selected workspace. Two workspaces therefore read and write different `.daily-roi` directories. Skill upgrades do not touch either directory. The default reset removes memory and current run only; audit removal requires the explicit `--include-audit` option.

Schemas: [`memory.schema.json`](../schemas/memory.schema.json), [`human-gate.schema.json`](../schemas/human-gate.schema.json), and [`run-state.schema.json`](../schemas/run-state.schema.json).
