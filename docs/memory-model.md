# Memory Layer v2

Local Memory is a small workspace-owned evidence store, not a free-form knowledge base. Shared Core rules, durable local facts, and current Run State are separate layers.

## Durable facts

Memory types are `ENTITY_MAPPING`, `PLAN_PATTERN`, `STORE_MAPPING`, and the narrowly reserved `WORKFLOW_PREFERENCE`. Every active fact records an explicit scope, human confirmation mode, original proposal, compact evidence summary, source run/decision, timestamps, and lineage. Rejected proposals live in a separate audit collection and set `creates_business_fact=false`.

The persistence policy first checks whether a decision is stable, target-identifiable, contradiction-free, and safely scoped. Current-day amounts, generic full-store filenames, and global allocation results are run-only even when the user asks to remember eligible facts. “全部接受” is run-only; “能记的记住” runs this eligibility policy.

## Scope and reuse

The minimum safe scope may include workspace, store, account, platform, template family, campaign namespace, or source type. A scoped hit is ignored outside that context. Before reuse, the resolver checks scope applicability, target existence, and current hard-identity contradictions.

Evidence precedence is:

1. current hard identity;
2. scoped historical human-confirmed memory;
3. current weak semantic inference.

Hard-identity conflict never allows memory to override the current target. The conflict is audited and the memory item transitions to `CONFLICTED` for human handling.

## Lifecycle and provenance

Lifecycle states are `ACTIVE`, `SUPERSEDED`, `CONFLICTED`, and `RETIRED`. A human correction creates a new active fact and links it to the superseded item; history is not erased. There is no fixed TTL. Invalidation is evidence-triggered by identity conflict, missing targets, scope change, explicit correction, or a newer deterministic relationship.

`REVIEW_ACCEPT` retains the AI proposal as the confirmed target. `HUMAN_CORRECTION` retains the rejected proposal but persists only the human-supplied target. A bare rejection creates audit provenance and returns the decision to unresolved state.

Lineage IDs prevent the same historical inference, its confirmation, and restated semantic evidence from being counted as independent support.

## Isolation and reset

The state root is `<workspace>/.daily-roi/`; separate workspaces do not share memory. Skill upgrades do not touch runtime state. The default reset removes memory and current run only; audit removal requires explicit `--include-audit`.

Schemas: [`memory.schema.json`](../schemas/memory.schema.json), [`human-gate.schema.json`](../schemas/human-gate.schema.json), and [`run-state.schema.json`](../schemas/run-state.schema.json).
