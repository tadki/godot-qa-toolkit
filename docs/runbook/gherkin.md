# gherkin — acceptance pipeline

## When to use

Turn `.feature` scenarios into executable acceptance verdicts: compile them to
a versioned IR and data-driven GUT tests, run them, then run a full-matrix
contract mutation over the IR to prove the assertions actually bite.

## Prerequisites

- The target project root contains `addons/gut/gut_cmdln.gd` for every write
  path (`compile --write` or `--keys`) and for `run` / `mutate` (a dry
  positional `compile` does not need it).
- Strict single root: `.feature` sources must be direct children of
  `addons/gqt_gherkin/features/`, and the harness loads only from
  `res://addons/gqt_gherkin/harness.gd`. Implement the interface in
  `docs/templates/testing_api.gd.md` (required methods: `smoke_check`,
  `init_case`, `run_step`, `query_state`, `check_expectations`, and `reset`). Missing
  harness bindings fail explicitly (`gherkin harness unavailable`), never
  silently skip. Other roots and symlink escapes are rejected.
- `schema.json` declares `gqt-gherkin-addon/1`, `requires_gqt`, and
  `testing_api`. `compile` performs this pure schema handshake without
  launching Godot or the validator; it does not require the harness
  implementation to run. `run` / `mutate` perform replay/domain checks first,
  then execute `addons/gqt_gherkin/validator.gd` before business GUT
  execution and require `supported_majors=[1]`.

## Typical examples

```bash
# Compile: .feature → IR + generated tests + manifest (--write is opt-in)
gqt gherkin compile addons/gqt_gherkin/features/foo.feature --project-root . --write

# Run: replay-check (three-artifact hash binding + recompile of the source
# .feature), then execute the generated suite
gqt gherkin run --project-root . --key foo

# Contract mutation: full-matrix IR mutants; survived = assertion gap
gqt gherkin mutate --project-root . --key foo

# Compile only selected features into a project-internal, empty caller-owned dir
# (dir = selection set; project-external suite dirs are rejected)
gqt gherkin compile --project-root . --keys save,load --out-dir .gqt-suite
gqt gherkin run --project-root . --suite-dir .gqt-suite

# W4 acceptance cost probe (compile+run wall clock)
gqt gherkin run --project-root . --gherkin-probe-keys save
```

## Output & exit codes

- `tool` is always `gherkin`; `ok` means this round passed.
- `compile`: positional single-feature `summary.wrote` lists all four files;
  `--keys` reports `suite_dir` plus `generated/test_<key>.gd` for each selected
  key. Default single-feature artifacts are
  `gherkin/ir/<key>.json`, `gherkin/generated/test_<key>.gd`,
  `gherkin/generated/gherkin_worker.gd`, and
  `gherkin/generated/<key>.manifest.json`; a private suite uses the same shape
  under `<suite-dir>/{ir,generated}`. The manifest binds the IR, generated
  test, and worker hashes. Feature keys normalize non-`[A-Za-z0-9_]`
  characters to `_` (`my-demo.feature` → `my_demo`).
- `run`: `summary.{replay_checked, ir_hash, exit_code, tests_executed, wrote}`;
  failing test names are attributed back to scenario/line through the
  manifest, and unattributable failures report `scenario: null` explicitly.
- `mutate`: `summary.{feature, feature_key, mutants, killed, survived,
  run_errors, full_matrix, worker, exempt_cases, exempt_results}`.
- exit `0` = pass; `1` = verdict fail / abort; `2` = argument error.

## Failure semantics

- Hash-binding mismatch, missing or drifted source `.feature`, and a missing
  `gherkin_worker.gd` / `worker_hash` → `run_error` (no leniency), including
  runs that do not pass `--worker`.
- Zero GUT test stream with exit 0 = verdict fail
  (`summary.tests_executed: 0` plus an explicit failure).
- `mutate`: any survived mutant rejects the run; zero Then bindings with zero
  unchanged slots and zero exemptions → `run_error`; any failing unmutated
  baseline case → fail-fast skip of the mutant round
  (`summary.exempt_baseline_failed: true`).
- Missing harness or failed `smoke_check` → explicit failure, no silent skip.

## Boundaries

- Only a strict Gherkin subset is accepted; the old Python steps runner and
  the `--steps` flag do not exist.
- Generated GUT tests have a **symmetric before/after lifecycle** (SEE-1619):
  once the harness is created, `after_each` restores state even on init
  failure, red assertions, early return, and for subsequent scenarios.
  Crash/timeout cleanup stays with the outer arm and is never reported as a
  successful reset.
- Default `--write` output goes only to `gherkin/ir|generated`
  (clean-then-generate unless `--no-clean`). `--keys` / `--out-dir` instead
  writes a project-internal private suite; an explicit non-empty caller-owned
  out-dir is rejected and is never cleared.
- `--suite-dir` must also stay inside the project. Containment and symlink-loop
  checks run before manifest reads, replay, validator, or Godot, and failures
  retain the single-JSON / no-traceback contract.
- Successful cleanup deletes only a tool-owned `.gqt-run-<token>` whose
  `.gqt-owner` is a regular UTF-8 file containing that same directory name and
  whose physical parent is exactly `<project>/gherkin/generated`. Copied,
  mismatched, symlinked, or non-UTF-8 markers are caller-owned and preserved;
  caller directories are never recursively deleted.
- Every validator, generated-GUT, and resident-worker Godot launch uses a fresh
  disposable HOME / XDG / TMPDIR environment, destroyed on exit. Under WSL,
  Linux-side variables remain absolute Linux paths; only Windows-side
  APPDATA / LOCALAPPDATA / TMP / TEMP use isolated UNC paths appended to WSLENV.
- Mutation here is **contract-level** (IR cell semantics), not source-level;
  source-level mutation uses `gqt mutation`.

## Command reference (aligned with v0.4.1 `--help`)

### gherkin compile

`gqt gherkin compile [feature] --project-root <root> [--write] [--no-clean] [--keys k1,k2] [--out-dir D]`

| Argument | Default | Meaning |
|---|---|---|
| `feature` (positional, optional) | — | Path to the `.feature` file; omit when `--keys` selects the private suite set |
| `--project-root <root>` | required | Target Godot project root (writes `gherkin/ir` + `gherkin/generated`) |
| `--write` | off | Opt-in artifact write for positional single-feature compilation; omitted = dry compile, summary only. `--keys` always writes its selected private suite and does not use this flag |
| `--no-clean` | off | Keep existing generated artifacts instead of clean-then-generate (positional single-feature compilation) |
| `--keys <k1,k2>` | omitted | Comma-separated feature keys compiled into a private suite dir (dir = selection set); pairs with `--out-dir` or creates a fresh `.gqt-run-<uuid>` dir |
| `--out-dir <dir>` | omitted | Project-internal private suite dir to write into instead of `gherkin/ir|generated`; an explicit non-empty caller-owned dir is rejected |

### gherkin run

`gqt gherkin run [directory] --project-root <root> [--key K] [--suite-dir D] [--gherkin-probe-keys [k1,k2]] [--timeout 120] [--godot-bin <path>]`

| Argument | Default | Meaning |
|---|---|---|
| `directory` (positional, optional) | `--project-root` | Alternative project root (another checkout containing `addons/gut/`) |
| `--project-root <root>` | required | Project root containing `addons/gut/` |
| `--key <feature-key>` | see note | Feature key the suite was compiled under; required without `--suite-dir` / `--gherkin-probe-keys` |
| `--suite-dir <dir>` | omitted | Run the whole project-internal private suite dir (gherkin-suite/1 reconciliation); external paths fail before replay/validator/Godot |
| `--gherkin-probe-keys [k1,k2]` | `aidomain_integration` | W4 cost probe: compile+run these keys via the private-dir pipeline and emit a `performance.gherkin_probe` record |
| `--timeout <s>` | 120 | GUT timeout seconds |
| `--godot-bin <path>` | omitted | Per-command Godot binary override; priority flag > env > PATH |

### gherkin mutate

`gqt gherkin mutate [directory] --project-root <root> --key <feature-key> [--timeout 120] [--worker] [--godot-bin <path>]`

| Argument | Default | Meaning |
|---|---|---|
| `directory` (positional, optional) | `--project-root` | Alternative project root |
| `--project-root <root>` | required | Project root containing `addons/gut/` |
| `--key <feature-key>` | required | Feature key the suite was compiled under |
| `--timeout <s>` | 120 | GUT timeout seconds per mutant round |
| `--worker` | off | Opt-in resident-worker channel: one godot process serves the whole matrix (stdin NDJSON + ir_file transfer; per-mutant release+rebuild+smoke probe, failure = run_error) |
| `--godot-bin <path>` | omitted | Per-command Godot binary override |
