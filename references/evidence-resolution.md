# Evidence Resolution v1.2 + Review UX v2

Unknown detection never goes directly to a Human Gate. Resolve every material nonzero entity in this order:

1. deterministic identity evidence;
2. structural evidence from the current template/file/store;
3. multi-evidence semantic and contextual resolution;
4. cross-file contradiction and global reconciliation checks;
5. search alternative complete solutions and contradictions;
6. classify the result as verified fact, reviewable inference, or unresolved human decision.

## Decision contract

Every resolution records `decision_type`, source(s), selected answer, evidence classes and supporting evidence, contradiction checks, alternatives, reconciliation result, reason, review risk, and whether human review is required. Semantic candidates also record their source scope and generation reason.

- `VERIFIED`: exact template name, unique template SKU/item ID, stable identity, confirmed run/Local Memory mapping, or a complete controlled deterministic structural relationship. It executes without review.
- `INFERRED_REVIEW`: the candidate is drawn only from the current TemplateModel, is unique, has semantic/structural/cross-file/reconciliation support, and survives contradiction checks. It is a proposal, not a verified fact; writing remains blocked until explicit acceptance or correction.
- `HUMAN_REQUIRED`: after relevant evidence is exhausted, no supported preferred candidate remains, current hard evidence conflicts with history, an external nonzero identity remains unresolved, or reconciliation has multiple/no complete solutions. Multiple candidates alone do not force this class when positive evidence establishes one reviewable preference.

No free-form probability or confidence number is an authorization to auto-resolve.

## Evidence rules

Strong identity evidence includes exact unique SKU, product ID, placement ID, stable platform item ID, and human-confirmed exact mappings. The source field is interpreted in its own identity namespace; a value must match the same namespace unless an exact same-row/cross-file bridge proves the namespaces refer to one product. A field label or cross-namespace value coincidence is never identity evidence. A duplicate identity assigned to different products is a contradiction and must not auto-resolve.

Structural resolution may map generic plans such as one-box/three-box markers to a uniquely known current-store main product. The file-to-store relationship and main product must already be uniquely established.

Semantic candidate generation uses the strongest available current-run boundary: current-store products first, then an explicitly bounded identity/file scope, and only then the current template when no narrower scope exists. It may use normalized roots, product-form normalization, containment, stable shared tokens, same-form variants, current-store scope, sibling alias families, exact-ID corroboration, and reconciliation. Each generated candidate must expose candidate, evidence, source scope, and reason. A one-Han-character edit or replacement is never positive lexical evidence; a literal short stem still requires contextual corroboration.

Before `HUMAN_REQUIRED`, the decision audit records exhaustion of relevant hard identity, Local Memory, TemplateModel, store/product scope, sibling/cross-file evidence, distribution, resolved files, financial remainder, global reconciliation, exclusivity, alternative solutions, and contradictions.

Global Constraint Resolution runs before a file-level store Gate when current files and the financial ledger form a complete constraint set. It uses exact integer cents, current file candidate stores/products, already attributed totals, and current ledger totals. Every participating file must contribute a non-amount product/identity constraint and must have no contradictory file evidence. A complete assignment with exactly one solution and zero differences becomes `INFERRED_REVIEW`, never `VERIFIED` merely because amounts reconcile. An amount-only coincidence, multiple solutions, missing coverage, inconsistent prior attribution, negative remainder, or nonzero difference remains `HUMAN_REQUIRED`. Filenames identify current-run occurrences only; historical filename ownership is never evidence.

## Evidence classes and review risk

- `HARD_IDENTITY`: exact stable identity or human-confirmed exact mapping; normally `VERIFIED`.
- `DETERMINISTIC_STRUCTURE`: a controlled template/store relationship whose prerequisites are complete; may be `VERIFIED`.
- `SEMANTIC_EVIDENCE`, `CROSS_FILE_EVIDENCE`, `GLOBAL_RECONCILIATION`, and `EXCLUSIVITY_EVIDENCE`: explain an inference but never impersonate identity evidence.

Review risk is a fixed enum derived from those classes. A unique semantic result with structural/cross-file/reconciliation support is low or medium review risk. Conflicts or unresolved alternatives are high risk and `HUMAN_REQUIRED`. No probability score is used.

Similarity alone is never identity and never authorizes deduplication.

## Human-question coalescing

Internal blockers are not user questions. Group aliases by independent business fact, not by record or internal family. Sources that share one supported target and one evidence explanation—including semantic siblings with different raw families—are presented once. A confirmed grouped mapping applies to every listed source; durable persistence still requires explicit human confirmation and eligibility.

Unreviewed inferred decisions are written only to the run audit and pending Review Batch. They never enter durable Local Memory.

## Batch review

Coalesce inferred records into independent business decisions. One supported semantic fact or one global allocation to the same target is shown once even when raw source families differ. The user may accept, correct, or reject every item in one batch; the runner mutates state once and resumes once. Acceptance upgrades provenance to `HUMAN_CONFIRMED`; eligible reusable mappings may enter Local Memory. Correction persists only the human-provided target. Rejection never persists the proposed target and converts the fact into an open Human decision on resume.
