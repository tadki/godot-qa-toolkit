# gherkin — acceptance pipeline

## When to use

Turn `.feature` scenarios into executable acceptance verdicts: compile them to
a versioned IR and data-driven GUT tests, run them, then run a full-matrix
contract mutation over the IR to prove the assertions actually bite.

## Prerequisites

- The target project root contains `addons/gut/gut_cmdln.gd` (`run` / `mutate`).
- One-time wiring: `res://addons/gqt_accept/harness.gd` must exist and
  implement the interface in `docs/templates/testing_api.gd.md` (the four-step
  combo `smoke_check` / `init_case` / `run_step` / `query_state` plus
  `check_expectations`). gqt never discovers or infers the adapter; when it is
  missing, generated tests fail explicitly (`acceptance harness unavailable`),
  never silently skip.
- `compile` also runs without wiring (it only writes artifacts).

## Typical examples

```bash
# Compile: .feature → IR + generated tests + manifest (--write is opt-in)
gqt gherkin compile acceptance/foo.feature --project-root . --write

# Run: replay-check (three-artifact hash binding + recompile of the source
# .feature), then execute the generated suite
gqt gherkin run --project-root . --key foo

# Contract mutation: full-matrix IR mutants; survived = assertion gap
gqt gherkin mutate --project-root . --key foo

# Compile only selected features into a one-off private dir (dir = selection set)
gqt gherkin compile --project-root . --keys save,load --out-dir /tmp/gqt-suite
gqt gherkin run --project-root . --suite-dir /tmp/gqt-suite

# W4 acceptance cost probe (compile+run wall clock)
gqt gherkin run --project-root . --acceptance-probe-keys save
```

## Output & exit codes

- `tool` is always `gherkin`; `ok` means this round passed.
- `compile`: `summary.wrote` per file; artifacts are
  `acceptance/ir/<key>.json`, `acceptance/generated/test_<key>.gd`,
  `<key>.manifest.json`.
- `run`: `summary.{replay_checked, ir_hash, exit_code, tests_executed, wrote}`;
  failing test names are attributed back to scenario/line through the
  manifest, and unattributable failures report `scenario: null` explicitly.
- `mutate`: `summary.{feature, feature_key, mutants, killed, survived,
  run_errors, full_matrix, worker, exempt_cases, exempt_results}`.
- exit `0` = pass; `1` = verdict fail / abort; `2` = argument error.

## Failure semantics

- Hash-binding mismatch, missing or drifted source `.feature` → `run_error`
  (no leniency).
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
- `--write` only writes the two `acceptance/ir|generated` directories
  (clean-then-generate); nothing else is written.
- Mutation here is **contract-level** (IR cell semantics), not source-level;
  source-level mutation uses `gqt mutation`.

## Command reference (aligned with v0.4.0 `--help`)

### gherkin compile

`gqt gherkin compile [feature] --project-root <root> [--write] [--no-clean] [--keys k1,k2] [--out-dir D]`

| Argument | Default | Meaning |
|---|---|---|
| `feature` (positional, optional) | — | Path to the `.feature` file; omit when `--keys` selects the private suite set |
| `--project-root <root>` | required | Target Godot project root (writes `acceptance/ir` + `acceptance/generated`) |
| `--write` | off | Opt-in artifact write; omitted = dry compile, summary only |
| `--no-clean` | off | Keep existing generated artifacts instead of clean-then-generate |
| `--keys <k1,k2>` | omitted | Comma-separated feature keys compiled into a private suite dir (dir = selection set); pairs with `--out-dir` or creates a fresh `.gqt-run-<uuid>` dir |
| `--out-dir <dir>` | omitted | Private suite dir to write into instead of `acceptance/ir|generated` |

### gherkin run

`gqt gherkin run [directory] --project-root <root> [--key K] [--suite-dir D] [--acceptance-probe-keys [k1,k2]] [--timeout 120] [--godot-bin <path>]`

| Argument | Default | Meaning |
|---|---|---|
| `directory` (positional, optional) | `--project-root` | Alternative project root (another checkout containing `addons/gut/`) |
| `--project-root <root>` | required | Project root containing `addons/gut/` |
| `--key <feature-key>` | see note | Feature key the suite was compiled under; required without `--suite-dir` / `--acceptance-probe-keys` |
| `--suite-dir <dir>` | omitted | Run the whole private suite dir (acceptance-suite/1 reconciliation) |
| `--acceptance-probe-keys [k1,k2]` | `aidomain_integration` | W4 cost probe: compile+run these keys via the private-dir pipeline and emit a `performance.acceptance_probe` record |
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
