# Evidence Resolution Layer v1

Unknown detection never goes directly to a Human Gate. Resolve every material nonzero entity in this order:

1. deterministic identity evidence;
2. structural evidence from the current template/file/store;
3. multi-evidence semantic and contextual resolution;
4. cross-file contradiction and reconciliation checks;
5. Human Gate only when the result remains ambiguous or contradictory.

## Decision contract

Every resolution records `source`, `candidate`, `decision`, evidence types, contradictions, alternatives, and reconciliation status.

- `VERIFIED`: exact template name, unique template SKU/item ID, stable identity, confirmed run mapping, confirmed Local Memory mapping, or a complete unique structural relationship.
- `MACHINE_INFERRED`: the candidate is drawn only from the current TemplateModel, is unique, has explicit semantic evidence plus independent context/corroboration where required, survives contradiction checks, and is reconciliation-consistent when reconciliation can discriminate.
- `HUMAN_REQUIRED`: multiple plausible candidates, conflicting evidence, weak semantics without enough independent support, an external nonzero identity, or an unresolved reconciliation consequence.

No free-form probability or confidence number is an authorization to auto-resolve.

## Evidence rules

Strong identity evidence includes exact unique SKU/item IDs and human-confirmed exact mappings. A duplicate template SKU assigned to different products is a contradiction and must not auto-resolve.

Structural resolution may map generic plans such as one-box/three-box markers to a uniquely known current-store main product. The file-to-store relationship and main product must already be uniquely established.

Semantic candidate generation is restricted to current template candidates. It may use normalized roots, product-form normalization, containment, stable shared tokens, a same-form single-character variant, current-store product scope, sibling alias families, exact-ID corroboration in another current file, and reconciliation. Weak one-character stems require at least two contextual supports.

Similarity alone is never identity and never authorizes deduplication.

## Human-question coalescing

Internal blockers are not user questions. Group unresolved aliases by entity type and normalized alias family. If several sources represent one business fact, emit one Gate containing all sources and occurrences. A confirmed grouped mapping applies to every listed source; durable persistence still requires explicit human confirmation.

Machine-inferred decisions are written only to the run audit. They never enter durable Local Memory.
