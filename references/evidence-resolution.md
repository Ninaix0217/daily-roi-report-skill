# Evidence Resolution Layer v1.1

Unknown detection never goes directly to a Human Gate. Resolve every material nonzero entity in this order:

1. deterministic identity evidence;
2. structural evidence from the current template/file/store;
3. multi-evidence semantic and contextual resolution;
4. cross-file contradiction and global reconciliation checks;
5. Human Gate only when the result remains ambiguous or contradictory.

## Decision contract

Every resolution records `source`, `candidate`, `decision`, evidence types, contradictions, alternatives, and reconciliation status. Semantic candidates also record their source scope and generation reason.

- `VERIFIED`: exact template name, unique template SKU/item ID, stable identity, confirmed run mapping, confirmed Local Memory mapping, or a complete unique structural relationship.
- `MACHINE_INFERRED`: the candidate is drawn only from the current TemplateModel, is unique, has explicit semantic evidence plus independent context/corroboration where required, survives contradiction checks, and is reconciliation-consistent when reconciliation can discriminate.
- `HUMAN_REQUIRED`: multiple plausible candidates, conflicting evidence, weak semantics without enough independent support, an external nonzero identity, or an unresolved reconciliation consequence.

No free-form probability or confidence number is an authorization to auto-resolve.

## Evidence rules

Strong identity evidence includes exact unique SKU, product ID, placement ID, stable platform item ID, and human-confirmed exact mappings. The source field is interpreted in its own identity namespace; a value must match the same namespace unless an exact same-row/cross-file bridge proves the namespaces refer to one product. A field label or cross-namespace value coincidence is never identity evidence. A duplicate identity assigned to different products is a contradiction and must not auto-resolve.

Structural resolution may map generic plans such as one-box/three-box markers to a uniquely known current-store main product. The file-to-store relationship and main product must already be uniquely established.

Semantic candidate generation uses the strongest available current-run boundary: current-store products first, then an explicitly bounded identity/file scope, and only then the current template when no narrower scope exists. It may use normalized roots, product-form normalization, containment, stable shared tokens, same-form variants, current-store scope, sibling alias families, exact-ID corroboration, and reconciliation. Each generated candidate must expose candidate, evidence, source scope, and reason. Weak one-character stems require at least two contextual supports.

Global Constraint Resolution runs before a file-level store Gate when current files and the financial ledger form a complete constraint set. It uses exact integer cents, current file candidate stores/products, already attributed totals, and current ledger totals. Every participating file must contribute a non-amount product/identity constraint and must have no contradictory file evidence. A file is `MACHINE_INFERRED` only when the complete assignment has exactly one solution and every store difference is zero. An amount-only coincidence, multiple solutions, missing coverage, inconsistent prior attribution, negative remainder, or any nonzero difference remains `HUMAN_REQUIRED`. Filenames identify current-run occurrences only; historical filename ownership is never evidence.

Similarity alone is never identity and never authorizes deduplication.

## Human-question coalescing

Internal blockers are not user questions. Group unresolved aliases by entity type and normalized alias family. If several sources represent one business fact, emit one Gate containing all sources and occurrences. A confirmed grouped mapping applies to every listed source; durable persistence still requires explicit human confirmation.

Machine-inferred decisions are written only to the run audit. They never enter durable Local Memory.
