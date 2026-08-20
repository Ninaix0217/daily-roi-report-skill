# Independent employee clean-room test

1. From the private GitHub repository, obtain tag `v0.1.0-rc.1` and copy or symlink the repository directory into your Codex skills directory.
2. Restart Codex if needed and confirm `$daily-roi-report-skill` appears in Skills or `/skills`.
3. Create a new directory with empty `input/` and `output/` folders. Do not reuse another employee's workspace.
4. Put only your own template and current-day source files in `input/`.
5. Ask Codex: `Use $daily-roi-report-skill with this input folder and write the result to output.`
6. If a Human Gate appears, answer only from your actual business knowledge. Mark reusable mappings as reusable; temporary exceptions as run-only.
7. Check the reconciliation summary and final workbook before using it operationally.
8. On the next day, reuse the same workspace and confirm previously approved reusable mappings resolve automatically. A different workspace should start with isolated memory.

If preflight reports missing LibreOffice, stop and install it through the normal IT process before processing legacy `.xls` files. The Skill does not download reports, log in to platforms, or use MCP.
