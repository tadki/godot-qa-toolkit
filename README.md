# godot-qa-toolkit

Deterministic five-stage QA toolchain for Godot / GDScript projects. Every
tool emits a machine-decidable verdict under the unified JSON contract:

```json
{"tool": "...", "ok": true, "summary": {...}, "failures": [...]}
```

Exit code semantics (consumers should only branch on `0` vs `!= 0`, then
parse stdout JSON to distinguish failure shapes):

| exit | meaning | stdout |
|---|---|---|
| `0` | verdict pass (`ok=true`) | JSON |
| `1` | verdict fail (`ok=false`): target failed, unmeasured, or aborted run | JSON |
| `2` | **caller error**: bad arguments / steps module failed to load | none (stderr only) |

Judgments are deterministic; no agent/heuristic scoring inside tool code.
The authoritative input/output contract (including per-subcommand field
lists, sentinel shapes, and the incremental-contract classification) is
[`docs/contract.md`](docs/contract.md) — treat that as the machine-consumable
source of truth.

## Tools

| Tool | Command | Status |
|---|---|---|
| Gherkin runner | `gqt gherkin <file.feature> [--steps <module.py or dir>] [--timeout S]` | shipped (M1) |
| Complexity gate | `gqt complexity <paths...> [--warn 10] [--max 15]` | shipped (M1) |
| Mutation runner | `gqt mutation <file.gd...> --project-root <root> [--budget 50] [--timeout S] [--tests <res://glob>] [--dry-run] [--jobs N]` | shipped (M2 + SEE-1321) |
| Coverage runner | `gqt coverage <file.gd> --project-root <root> [--min-percent 80] [--timeout S]` | shipped (M2 + SEE-1312) |
| Godot determinism | — | planned (logic/scheduling here; deterministic commands register into the godot-mcp ① runtime addon) |

`mutation` and `coverage` share three mandatory preconditions:

- `--project-root` must point at a Godot project root containing
  `addons/gut/gut_cmdln.gd` — the tools consume the **tested project's own
  GUT**, they ship zero Godot assets of their own;
- a `godot` executable must be on `PATH`;
- `--project-root` is the *only* channel through which the test subject
  enters the tool: zero discovery, zero environment guessing, zero writes
  outside the target `.gd` (always restored) and the coverage probe sink
  under `user://`.

> The godot-mcp control plane (launcher/proxy/shim/port arbitration) belongs
> to the **godot-mcp runtime library** (①), not here. This library is the
> tool-state half of a two-library split: run-time driving (①) vs test means
> (②, this repo).

## Gherkin (self-authored)

Minimal, strict Gherkin subset: `Feature`, `Background`, `Scenario`,
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
  must not misreport "environment not ready" as "code regressed".

## Complexity gate (via gdtoolkit)

Function-level cyclomatic complexity through gdtoolkit's parser + radon.
Files gd2py cannot convert (GDScript identifiers colliding with Python
keywords) are reported as structural failures — unmeasured code is a fail
signal, never a silent skip.

```bash
gqt complexity src/systems autoload/vendor.gd --warn 10 --max 15
```

Semantics: `cc > max` → failure; `warn < cc <= max` → warning (reported,
not failing); unparseable file → failure. Lowering `--max` below `--warn`
collapses warn into max. `ok = no violation AND no unparseable` — warnings
alone still pass.

## Mutation runner

Runs baseline GUT on the untouched source, then per-mutant rewrites the
file (restoring it after every run, SIGTERM/atexit cleanup included — an
interrupted run leaves no `.gqt_mutation_*` temp files behind).

```bash
gqt mutation src/save/save_manager.gd --project-root /path/to/project \
    --budget 50 --timeout 240
```

Key semantics (deterministic, see `docs/contract.md` §4.3 for the full
field contract):

- **Kill verdicts are set-differenced, not exit-code based**: kill = the set
  of *newly failing* test names relative to baseline, so pre-existing
  project failures are never misread as kills.
- Mutations are source-level (AST-derived) operator swaps: `AOR`, `ROR`,
  `UOI`, boundary flips, ternary/guard-notation/logical/membership/`is`
  variants — `kind` is an **open enum**, filter by prefix, tolerate unknown
  values. Product generates *invalid* mutants (python syntax check) are
  dropped before running, not scored against the suite.
- Baseline itself untrustable / timed out → unified **abort shape**
  (`summary.run_error: true`, no killed/survived keys — consumers must not
  default-read them).
- Per-mutant timeout auto-adapts to `baseline wall-clock × 2` (only
  widened, never tightened; visible as `summary.adaptive_timeout_s`).
- `--tests <res://glob>` scopes GUT to an affected-test directory;
  `--dry-run` only counts mutation sites without running GUT.
- `--jobs N` evaluates multiple target files (or a single file with
  explicit `--jobs`) concurrently across per-file workers.
- `ok = survived == 0 AND timeout == 0 AND run_errors == 0 AND suspect == 0`
  (`suspect` = cannot prove the mutant was killed — gated same as
  survived). Reports on real projects: mutation 53/55 killed with 2
  surviving mutants adjudicated as recorded equivalents.

## Coverage runner

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

- instrumentation producing unparseable source → generation-time
  `run_error` (refuses to run fake data);
- GUT exited but probes never fired → explicit `probes_fired: 0`
  annotation with `ok=false` — "probes never fired" and "true 0% coverage"
  must remain distinguishable;
- a file with zero executable lines → `summary.vacuous: true` (empty-set
  coverage is not full-score evidence);
- branch coverage v1 = derivation-layer only (`branch_total` /
  `branch_covered` / `branch_coverage_percent`): does a branch *execute*,
  not "is it asserted on" — observational, does not enter `ok`;
- `ok = coverage_percent >= min_percent`, forced false on `probes_fired=0`
  or `run_error`.

## Install & multi-worktree safety

```bash
bash scripts/install.sh     # pips this tree (PEP 668 retry included)
pip install -e .
pytest
```

`scripts/install.sh` pins the system `gqt` editable install to **this**
checkout. On a machine with several godot-qa-toolkit trees (typical when the
toolkit is mounted as `.dev/qa-toolkit` submodule in multiple task
worktrees), a stale editable egg-link silently makes `gqt` run old tool code
— which coverage/mutation verdicts cannot tolerate. `gqt doctor` compares
the editable install location against the current tree's `.dev/qa-toolkit`
root and exits 2 with a repair hint on mismatch; re-run `install.sh` after
every parent-repo gitlink bump.

### PYTHONPATH convention (SEE-1308 §SPEC-003)

pytest that must run against a specific tree pins it explicitly instead of
trusting the editable install:

```bash
cd <this-worktree>/.dev/qa-toolkit
PYTHONPATH="$PWD/src" python -m pytest tests/ -q
```

Re-point a drifting editable install from the *current* tree:

```bash
cd <this-worktree>/.dev/qa-toolkit && pip install -e .
```

The rule: never rely on an editable install whose egg-link you did not just
create from the tree you are testing.
