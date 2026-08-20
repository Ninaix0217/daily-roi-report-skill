# Template and workbook protection

## Dynamic TemplateModel

The workbook worker discovers at runtime:

- sheet names and workbook order;
- the report table from semantic headers;
- product rows, total row, date cell, writable cost/sales/ROI cells, and existing formula precedent;
- SKU-to-product relationships, SKU specification order, and conflicting duplicate SKU assignments;
- store/product group structures on supporting sheets;
- merged cells, widths, heights, and style/layout fingerprints.

No sheet name, range, product count, SKU count, product order, or store set is a production constant.

An exact SKU/item ID is `VERIFIED` only when the TemplateModel maps it to one product. The same identifier assigned to different template products is an explicit contradiction and requires resolution before writing.

## Protected write

Writing starts only after no unresolved gates remain and reconciliation passes. The worker imports the original template, updates only cells identified by `TemplateModel`, and exports a new `.xlsx`.

- Cost cells use auditable additive formulas when multiple source components exist.
- Product real-sales formulas preserve the template's SKU formula order.
- Product ROI is `IF(cost=0,0,real_sales/cost)`.
- Total cost and sales use `SUM` over the discovered product region.
- Total ROI divides total real sales by total cost; it never averages product ROI.

## Deterministic verification

The verifier checks expected values/formulas, error tokens, sheet names/order, product order, merged ranges, column widths, row heights, and non-writable style changes. It renders every sheet when supported and reports `RENDERED_UNREVIEWED` until a human/model actually inspects the images.

Legacy `.xls` input is copied byte-for-byte to a short temporary path, converted non-interactively with LibreOffice to `.xlsx`, then inspected. The original file is never modified, and its source SHA-256 remains in the manifest.
