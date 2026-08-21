# Daily ROI Report Skill v0

This standalone Codex Skill turns an existing daily ROI Excel workflow into a repeatable local-file process. It discovers the current template structure, resolves unknown entities through auditable identity/structural/context evidence, reconciles paid advertising cost and optional sales data, pauses only when material evidence remains ambiguous, remembers only reusable human confirmations in the employee's own workspace, preserves the workbook layout, and produces a verified `.xlsx` copy.

The shared Skill contains workflow and verification logic only. It does not ship employee store aliases, product aliases, campaign shorthand, SKUs, dates, or business amounts.

## Prerequisites

- ChatGPT desktop with Codex, Codex CLI, or the Codex IDE extension.
- The bundled Codex Python/Node workspace dependencies used by spreadsheet Skills.
- LibreOffice when an input is legacy `.xls`. Modern `.xlsx` and `.csv` inputs do not require LibreOffice conversion.

The Skill never downloads or installs dependencies. If a required dependency is missing, preflight stops before business files are processed and reports the missing component.

## Install from GitHub

Clone or download the selected GitHub release tag. Keep the repository checkout separate from the employee's daily report workspace and pin employee acceptance to that tag rather than moving `main`.

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

On a first run, the Skill first attempts deterministic identity, template structure, semantic/context corroboration, contradiction checks, and reconciliation. Strong identity facts are verified automatically. A unique evidence-backed inference is proposed in one batch for quick accept/correct review; only a fact that remains genuinely ambiguous produces an open Human Gate. Accepted or corrected reusable mappings are remembered and automatically applied in later runs in that same workspace.

For direct CLI use:

```powershell
python scripts/daily_roi.py run --workspace <WORKSPACE> --input-dir <INPUT_DIR> --output-dir <OUTPUT_DIR> --node <NODE> --node-modules <NODE_MODULES>
```

Use `status`, `memory`, `review`, and `resolve` subcommands to inspect or resume a run. `review --reply "全部接受"` approves only the displayed batch for the current run; wording such as “能记的记住” persists only mappings that pass durability and scope policy. Numbered natural corrections, `--accept-all`, and `--responses-json <file>` are also supported.

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
