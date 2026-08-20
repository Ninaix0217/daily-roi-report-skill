# Output contract

For a completed run, report:

```text
Target Date: YYYY-MM-DD
Input: <count> files
Preflight: PASS
Mappings: VERIFIED=<count>, INFERRED_REVIEW=<count>, HUMAN_REQUIRED=<count>, human-confirmed=<count>
Review: accepted=<count>, rejected=<count>, corrected=<count>, open-ended decisions=<count>
Expense Reconciliation: PASS|FAIL; difference = 0.00
Sales Reconciliation: PASS|NOT PROVIDED|FAIL
Duplicate Evidence: <summary>
Output Validation: PASS|FAIL
Visual Verification: PASS|RENDERED_UNREVIEWED|UNAVAILABLE
Result: <output filename>
```

Also list runtime-discovered multi-product store totals, product splits, split totals, and differences. State which columns were intentionally left unfilled and why. Link the output workbook exactly once.

Before completion, present three distinct sections: **Verified Automatically**, **AI Proposed — Needs Review**, and **Needs Your Decision**. Show inferred families as proposed answers with concise supporting evidence, never as open-ended questions. No final workbook may exist while either of the latter two sections has unresolved items.
