# Daily ROI Report Skill v0

This standalone Codex Skill turns an existing daily ROI Excel workflow into a repeatable local-file process. It discovers the current template structure, reconciles paid advertising cost and optional sales data, pauses on material unknowns, remembers only reusable human confirmations in the employee's own workspace, preserves the workbook layout, and produces a verified `.xlsx` copy.

The shared Skill contains workflow and verification logic only. It does not ship employee store aliases, product aliases, campaign shorthand, SKUs, dates, or business amounts.

## Prerequisites

- ChatGPT desktop with Codex, Codex CLI, or the Codex IDE extension.
- The bundled Codex Python/Node workspace dependencies used by spreadsheet Skills.
- LibreOffice when an input is legacy `.xls`. Modern `.xlsx` and `.csv` inputs do not require LibreOffice conversion.

The Skill never downloads or installs dependencies. If a required dependency is missing, preflight stops before business files are processed and reports the missing component.

## Install from GitHub

Clone or download the selected private GitHub release tag. Keep the repository checkout separate from the employee's daily report workspace.

Install by copying or symlinking this directory to a Codex skill discovery location:

```text
<workspace>/.agents/skills/daily-roi-report-skill
```

or:

```text
~/.agents/skills/daily-roi-report-skill
```

Restart Codex if the skill is not discovered immediately. The runtime requires the Codex bundled Python/Node dependencies and a local LibreOffice installation for legacy `.xls` conversion.

Confirm discovery by opening the Skills list, running `/skills` in Codex CLI/IDE, or typing `$daily-roi-report-skill` in a prompt. Codex should show the Skill name and description.

## Create a daily workspace

Create a new employee-owned directory, for example:

```text
daily-roi-workspace/
  input/
  output/
```

Place the employee's current template and that day's source exports in `input/`. Do not put business files inside the installed Skill repository. The Skill writes the completed workbook to `output/` and runtime audit state to `daily-roi-workspace/.daily-roi/`.

## Dependency preflight

Codex normally runs dependency preflight automatically. For direct CLI diagnosis:

```powershell
python scripts/daily_roi.py preflight --input-dir <INPUT_DIR> --node <NODE> --node-modules <NODE_MODULES>
```

For legacy `.xls`, a missing LibreOffice installation returns a clear result such as:

```text
DEPENDENCY_CHECK = FAIL
MISSING = LibreOffice
```

Install LibreOffice through the employee's normal IT process, then rerun. The Skill will not install it or modify the system.

## Run

In the employee's clean workspace, ask Codex to use `$daily-roi-report-skill`, then provide the `input/` and `output/` locations. The Skill performs discovery, dependency preflight, reconciliation, protected writing, deterministic verification, and sheet rendering.

On a first run, an unknown nonzero mapping produces a structured Human Gate. After the user confirms it, Codex classifies the answer as reusable or run-only, records it, and resumes the same run. A reusable confirmation is automatically applied in later runs in that same workspace.

For direct CLI use:

```powershell
python scripts/daily_roi.py run --workspace <WORKSPACE> --input-dir <INPUT_DIR> --output-dir <OUTPUT_DIR> --node <NODE> --node-modules <NODE_MODULES>
```

Use `status`, `memory`, and `resolve` subcommands to inspect or resume a run.

## Local memory

Employee-owned state lives at:

```text
<workspace>/.daily-roi/
  memory.json
  confirmations.jsonl
  current-run.json
  runs/
```

Installing or upgrading the Skill does not overwrite this directory. Different workspaces have isolated memory.

To remove learned mappings and the current run:

```powershell
python scripts/daily_roi.py reset-memory --workspace <WORKSPACE>
```

Add `--include-audit` only when the user explicitly wants confirmation history and run audits removed as well.

## Safety and limits

- Source files and the template are never overwritten.
- Writing is blocked until all material gates are resolved and all reconciliations equal zero to the cent.
- Similar-looking records are not duplicates unless identity evidence proves they represent the same underlying event.
- Currency truth uses Decimal/integer cents.
- Template structure is discovered dynamically; unseen templates still must contain recognizable semantic headers and formula precedents.
- Visual PASS requires actual inspection of every rendered sheet; rendering without inspection is reported honestly.
- No downloading, browser automation, platform login, production API, MCP, shared-memory service, or cloud telemetry is included.

See the short [clean-room test guide](docs/clean-room-test-guide.md), [architecture](docs/architecture.md), [memory model](docs/memory-model.md), and [validation](docs/validation.md).
