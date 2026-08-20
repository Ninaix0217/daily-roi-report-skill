# Output contract

For a completed run, report:

```text
Target Date: YYYY-MM-DD
Input: <count> files
Preflight: PASS
Mappings: VERIFIED=<count>, MACHINE_INFERRED=<count>, human-confirmed=<count>, unresolved=<count>
Expense Reconciliation: PASS|FAIL; difference = 0.00
Sales Reconciliation: PASS|NOT PROVIDED|FAIL
Duplicate Evidence: <summary>
Output Validation: PASS|FAIL
Visual Verification: PASS|RENDERED_UNREVIEWED|UNAVAILABLE
Result: <output filename>
```

Also list runtime-discovered multi-product store totals, product splits, split totals, and differences. State which columns were intentionally left unfilled and why. Link the output workbook exactly once.

For `MACHINE_INFERRED`, retain the evidence result in the run audit. In the user summary, describe only material inferred families; do not ask the user to confirm decisions that already passed the evidence policy.
