<div align="center">

# godot-qa-toolkit

**Deterministic five-stage QA toolchain for Godot / GDScript projects.**

Every tool emits a machine-decidable verdict under one unified JSON contract —
exit code `0` = pass, `1` = fail. No agent scoring, no heuristics inside tool
code: judgments are deterministic by design (the P0' principle).

[![ci-pytest](https://github.com/tadki/godot-qa-toolkit/actions/workflows/ci-pytest.yml/badge.svg)](https://github.com/tadki/godot-qa-toolkit/actions/workflows/ci-pytest.yml)
[![ci-devtests](https://github.com/tadki/godot-qa-toolkit/actions/workflows/ci-devtests.yml/badge.svg)](https://github.com/tadki/godot-qa-toolkit/actions/workflows/ci-devtests.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)

</div>

---

## The unified JSON contract

Every tool speaks the same language, so anything (agent, CI job, shell
pipeline) can consume a verdict without tool-specific glue:

```json
{"tool": "...", "ok": true, "summary": {...}, "failures": [...]}
```

**Exit code semantics** — consumers branch only on `0` vs `!= 0`, then parse
stdout JSON to distinguish failure shapes:

| exit | meaning | stdout |
|---|---|---|
| `0` | verdict pass (`ok=true`) | JSON |
| `1` | verdict fail (`ok=false`): target failed, unmeasured, or aborted run | JSON |
| `2` | **caller error**: bad arguments / steps module failed to load | none (stderr only) |

The authoritative input/output contract (per-subcommand field lists, sentinel
shapes, incremental-contract classification) is
[`docs/contract.md`](docs/contract.md) — treat it as the machine-consumable
source of truth.

## Tools at a glance

| Tool | Command | Status |
|---|---|---|
| **Gherkin runner** | `gqt gherkin <file.feature> [--steps <module.py or dir>] [--timeout S]` | shipped (M1) |
| **Complexity gate** | `gqt complexity <paths...> [--warn 10] [--max 15]` | shipped (M1) |
| **Mutation runner** | `gqt mutation <file.gd...> --project-root <root> [--budget 50] [--timeout S] [--tests <res://glob>] [--dry-run] [--jobs N]` | shipped (M2 + SEE-1321) |
| **Coverage runner** | `gqt coverage <file.gd> --project-root <root> [--min-percent 80] [--timeout S]` | shipped (M2 + SEE-1312) |
| **Godot determinism** | logic/scheduling here; deterministic commands register into the godot-mcp ① runtime addon | planned |

`mutation` and `coverage` share three mandatory preconditions:

- `--project-root` must point at a Godot project root containing
  `addons/gut/gut_cmdln.gd` — the tools consume the **tested project's own
  GUT**, they ship zero Godot assets of their own;
- a `godot` executable must be on `PATH`;
- `--project-root` is the *only* channel through which the test subject
  enters the tool: zero discovery, zero environment guessing, zero writes
  outside the target `.gd` (always restored) and the coverage probe sink
  under `user://`.

> **Two-library split:** the godot-mcp control plane (launcher / proxy / shim /
> port arbitration) belongs to the **godot-mcp runtime library** (①). This repo
> is the tool-state half (②): *run-time driving* lives in ①, *test means* live
> here.

## Quick start

```bash
bash scripts/install.sh     # pips this tree (PEP 668 retry included)
pytest                      # run the suite
gqt doctor                  # verify your install + toolchain wiring
```

Then pick a tool:

```bash
# 1. Run a Gherkin feature against a Python step registry
gqt gherkin features/leaderboard.feature --steps steps.py

# 2. Gate cyclomatic complexity
gqt complexity src/systems --warn 8 --max 12

# 3. Mutation-test two scenes in one process, 8 workers
gqt mutation scenes/ui/a.gd scenes/ui/b.gd --project-root . --jobs 8
```

---

## Gherkin runner (self-authored)

A minimal, **strict** Gherkin subset: `Feature`, `Background`, `Scenario`,
`Scenario Outline`, tags (`@tag`), and `Given/When/Then/And/But` steps.
`And`/`But` inherit the previous keyword. Anything unparseable is an error
with `path:line` context — a spec that only "mostly" parses is not
machine-checkable.

Steps bind to a Python registry:

```python
# steps.py
from godot_qa_toolkit.gherkin.runner import Registry

def register(r: Registry):
    @r.step("{n} hearts")
    def hearts(ctx, n):
        ctx["hearts"] = int(n)
```

```bash
gqt gherkin features/leaderboard.feature --steps steps.py
```

- `--steps` accepts **one module file or a directory**: for a directory,
  every public `*.py` (underscore-prefixed files are treated as shared
  helpers and skipped) is loaded as a steps module.
- `--timeout S` puts a per-scenario wall-clock budget on step execution.
- A steps module that cannot import (missing helper, missing dependency,
  missing game-side import cache) is a **caller error, exit 2** — the CLI
  never misreports "environment not ready" as "code regressed".

## Complexity gate (via gdtoolkit)

Function-level cyclomatic complexity through gdtoolkit's parser + radon.
Files gd2py cannot convert (GDScript identifiers colliding with Python
keywords) are reported as structural failures — unmeasured code is a fail
signal, never a silent skip.

```bash
gqt complexity src/systems autoload/vendor.gd --warn 10 --max 15
```

Semantics: `cc > max` → failure; `warn < cc <= max` → warning (reported, not
failing); unparseable file → failure. Lowering `--max` below `--warn`
collapses warn into max. `ok = no violation AND no unparseable` — warnings
alone still pass.

## Mutation runner (SEE-1400 / SEE-1413)

Runs baseline GUT on the untouched source, then per-mutant rewrites the file
(restoring it after every run, SIGTERM/atexit cleanup included — an
interrupted run leaves no `.gqt_mutation_*` temp files behind).

```bash
gqt mutation src/save/save_manager.gd --project-root /path/to/project \
    --budget 50 --timeout 240
```

Key semantics (deterministic, see `docs/contract.md` §4.3 for the full field
contract):

- **Kill verdicts are set-differenced, not exit-code based**: kill = the set
  of *newly failing* test names relative to baseline, so pre-existing project
  failures are never misread as kills.
- Mutations are source-level (AST-derived) operator swaps: `AOR`, `ROR`,
  `UOI`, boundary flips, ternary/guard-notation/logical/membership/`is`
  variants — `kind` is an **open enum**, filter by prefix, tolerate unknown
  values. Product generates *invalid* mutants (python syntax check) are
  dropped before running, not scored against the suite.
- Baseline itself untrustable / timed out → unified **abort shape**
  (`summary.run_error: true`, no killed/survived keys — consumers must not
  default-read them).
- Per-mutant timeout auto-adapts to `baseline wall-clock × 2` (only widened,
  never tightened; visible as `summary.adaptive_timeout_s`).
- `--tests <res://glob>` scopes GUT to an affected-test directory;
  `--dry-run` only counts mutation sites without running GUT.
- `ok = survived == 0 AND timeout == 0 AND run_errors == 0 AND suspect == 0`
  (`suspect` = cannot prove the mutant was killed — gated same as survived).

### Survivor location in the report (SEE-1416)

Every verdict record (in `failures`, killed records included) now carries
`column`, `operator`, and `line_text` (the exact source line that was
mutated) on top of the existing `file/line/kind/original/mutated/verdict/
reason` — locate the failure without opening the source file.

`.summary` is self-sufficient too: it carries `survivors` (top ≤5 non-killed
records in a compact `{file,line,column,kind,original,mutated,verdict,reason}`
projection) with `survivors_truncated: true` when more exist; the key is
always present (empty list when nothing survived). Slicing with
`| jq '.summary'` therefore no longer loses the location detail:

```bash
gqt mutation systems/world/occupancy_map.gd --project-root .     --budget 50 --timeout 120 | jq '.summary'
```

Multi-file runs add a top-level `summary.attention` on the envelope
(cross-file merged non-killed details, same record shape, ≤5 entries);
single-file flat contract output is unchanged apart from the new fields
(additive only — no existing field changed meaning).

### Concurrency & resume

**Prefer one gqt process over many files.** `gqt mutation a.gd b.gd` splits
mutants across both files inside one process (the `--jobs` worker pool
balances per-file and per-mutant slices), so a single command is the fastest
correct shape:

```bash
gqt mutation scenes/ui/a.gd scenes/ui/b.gd --project-root . \
    --tests res://tests/unit/ui/ --jobs 8
```

**Single files go parallel too (SEE-1413).** `--jobs` slices a single file
per-mutant as well, and it is on by default (`max(2, cpu//2)`):

```bash
gqt mutation <file> --project-root . --jobs 8
```

Omitting `--jobs` uses the default pool; a file whose mutation count is ≤
jobs automatically falls back to serial (no prewarm, no shared-baseline
pre-run — the fixed overhead would outweigh the parallelism); `--jobs 1`
always forces serial.

<details>
<summary><strong>Concurrency knobs & resume semantics</strong></summary>

- `--jobs N` — per-process worker pool, clamped to `max(2, cpu//2)`.
- `GQT_GLOBAL_GODOT_SLOTS` — **cross-process** cap on live godot subprocesses
  (default `max(2, cpu//2)`). Every GUT run (baseline + per-mutant, and
  coverage) takes a flock slot under `.qa-cache/gqt-godot-slots/`; slots are
  released automatically when a process dies. Running two gqt processes in
  parallel? Lower this (or `--jobs`) so the total never exceeds your cores —
  two un-capped processes at 8 workers each = 16 godot instances fighting
  over 16 cores. On platforms without `fcntl` the gate warns once and steps
  aside (never blocks).
- `--resume` — every run already writes each mutant's verdict to
  `.qa-cache/gqt-mutation-resume/<fingerprint>/` as it lands. If a run is
  cancelled, rerun the same command with `--resume`: completed mutants are
  skipped and the merged report is identical in shape to a full run
  (`summary.resumed` counts the skips). The fingerprint is
  `sha1(target realpath + source sha1 + effective tests_glob + budget + ops
  version)` — change any component and the stale verdicts are ignored
  (re-run, never dirty-merged). A corrupt or unreadable store degrades
  honestly to "no history". `--dry-run` stays zero-IO.

Auto-scan note (SEE-1400 §SPEC-005): when a mutation target is referenced by
exactly one test file, the scanner now scopes to that file (GUT `-gselect`)
instead of its directory — same conservative direction (narrower can only
over-report survivors, never fake kills), cheaper per round.

</details>

## Coverage runner (SEE-1312)

AST statement-level instrumentation of a single `.gd` file: probe calls
inserted per executable statement (ID-keyed data plane: integer probe id →
line manifest), generation-time re-parse precheck, then one headless GUT
pass, and the hit sink read back from `user://qa-coverage-hits-<pid>.txt`
(run-unique, `user://` first with project-root fallback).

```bash
gqt coverage src/save/save_manager.gd --project-root /path/to/project \
    --min-percent 80
```

Capability and sentinels (see `docs/contract.md` §4.4):

- instrumentation producing unparseable source → generation-time `run_error`
  (refuses to run fake data);
- GUT exited but probes never fired → explicit `probes_fired: 0` annotation
  with `ok=false` — "probes never fired" and "true 0% coverage" must remain
  distinguishable;
- a file with zero executable lines → `summary.vacuous: true` (empty-set
  coverage is not full-score evidence);
- branch coverage v1 = derivation-layer only (`branch_total` /
  `branch_covered` / `branch_coverage_percent`): does a branch *execute*, not
  "is it asserted on" — observational, does not enter `ok`;
- `ok = coverage_percent >= min_percent`, forced false on `probes_fired=0` or
  `run_error`.

---

## Development

```bash
bash scripts/install.sh     # pips this tree (PEP 668 retry included)
pytest
```

`scripts/install.sh` pins the system `gqt` editable install to **this**
checkout. On a machine with several godot-qa-toolkit trees, a stale editable
egg-link silently makes `gqt` run old tool code — which coverage/mutation
verdicts cannot tolerate. `gqt doctor` compares the editable install location
against the current tree and reports mismatches with a repair hint; re-run
`install.sh` after every checkout switch.

<details>
<summary><strong>PYTHONPATH convention (multi-workdir safety, SEE-1308 §SPEC-003)</strong></summary>

When several KOL worktrees live on the same machine and share one Python
site-packages, a `pip install -e .` from an older checkout leaves an
`__editable__.godot_qa_toolkit-0.1.0.pth` egg-link that points at that old
tree. pytest then resolves `import godot_qa_toolkit` against the stale source
and can fail spuriously even though the current tree is fine.

Two belts guard against this:

1. **`--import-mode=importlib`** — already set in `pyproject.toml` under
   `[tool.pytest.ini_options].addopts`. It makes pytest resolve the package by
   module path rather than trusting the egg-link, so a wrong-root egg-link
   cannot break the suite's own import.
2. **Explicit `PYTHONPATH`** — when you must run against a specific tree, pin
   it instead of relying on the editable install:

   ```bash
   cd <gqt-checkout-root>
   PYTHONPATH="$PWD/src" python -m pytest tests/ -q
   ```

   Re-point a drifting editable install from the *current* tree:

   ```bash
   cd <gqt-checkout-root> && pip install -e .
   ```

   The rule: never rely on an editable install whose egg-link you did not just
   create from the tree you are testing.

</details>

<details>
<summary><strong>Standalone install mode (SEE-1396)</strong></summary>

gqt no longer lives under a KOL `.dev/qa-toolkit` submodule — it is an
independent CLI installed from a standalone checkout (e.g. a persistent
`~/tools/godot-qa-toolkit` on WSL). `gqt doctor` works out of the box in
standalone mode (skips the root comparison when it cannot resolve a target
tree — exit 0, never a false report). To have doctor compare the editable
install against a specific tree, point it there:

```bash
GQT_TOOLKIT_ROOT=~/tools/godot-qa-toolkit gqt doctor
```

</details>

## Design principles

- **Deterministic verdicts** (P0' principle) — every tool's outcome is fully
  reproducible; no agent or heuristic scoring inside tool code.
- **One JSON contract** — uniform `{tool, ok, summary, failures}` output and
  `0`/`1` exit codes across the whole toolchain.
- **Unmeasured is a fail** — unconvertible code, unscanned targets and silent
  skips are reported as structural failures, never swallowed.
- **Honest degradation** — caches, locks and resume stores degrade to safe,
  explicitly-reported behavior instead of wrong answers.
