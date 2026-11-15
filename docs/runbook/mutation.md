# mutation — mutation testing

## When to use

Verify that your test suite actually catches source defects: generate
source-level mutants for a `.gd` file, execute each in an isolated shadow slot,
and report every mutant the tests fail to kill as a survivor (an assertion
gap).

## Prerequisites

- `<root>/addons/gut/gut_cmdln.gd` exists; a resolvable Godot binary
  (`--godot-bin` > `GQT_GODOT_BIN` > PATH).
- With `--tests` omitted, selection uses the runtime truth map
  (`test-attribution/5`): by default it is auto-managed at
  `.qa-cache/attribution-map.json` (generated / incrementally refreshed once
  when missing). You can instead pass an explicit read-only artifact with
  `--attribution-map`, or build one with `gqt measure map-update`.

## Typical examples

```bash
# Default: function-level truth-map selection + differential resume +
# coverage prefilter (all automatic)
gqt mutation src/save/save_manager.gd --project-root . --budget 50

# Selection granularity: fn (default) / file (per-file set) /
# full (explicit full-face audit, bypasses the mapping)
gqt mutation src/save/save_manager.gd --project-root . --select file

# Explicit test subset (bypasses the mapping entirely)
gqt mutation src/save/save_manager.gd --project-root . --tests res://tests/save/

# Count sites only; single-site rerun (bypasses diff reuse and the prefilter)
gqt mutation src/save/save_manager.gd --project-root . --dry-run
gqt mutation src/save/save_manager.gd --project-root . --mutation "save_manager#func:save#2"
gqt mutation src/save/save_manager.gd --project-root . --lines 42

# Parallelism (default jobs = max(2, cpu//2)); force serial with --jobs 1
gqt mutation <file> --project-root . --jobs 8

# Opt-outs for troubleshooting
gqt mutation <file> --project-root . --no-attribution-auto
gqt mutation <file> --project-root . --no-reuse-coverage
GQT_MUTATION_WORKER=0 gqt mutation <file> --project-root .
```

## Selection semantics

- `fn` (default): mutants are grouped by enclosing function and each group runs
  the mapping's test set; a function missing from the mapping falls back to the
  file's bounded set (`file_fallback` disclosed); when the file level itself is
  unusable the run **fails loud** — no automatic full-face fallback.
- Freshness: the mapping's `report_recipe_hash` must match the current tree and
  the target file's sha256 must match the mapping's binding. In auto mode a
  stale or missing map is regenerated once, then re-judged; if still unusable,
  execution is refused.
- Zero references: a fresh mapping that says no test touches the target is a
  terminal short-circuit (`summary.zero_ref_short_circuit=true`, zero
  execution, `ok=true` disclosure) — not a full-suite fallback.
- `full`: the only explicit audit face (`tests_glob=None`); use only when you
  deliberately want the whole suite.

## Output & exit codes

- `tool` is always `mutation`.
  `ok = survived==0 ∧ timeout==0 ∧ run_errors==0 ∧ suspect==0`;
  `invalid_mutant`, `timeout_pinned` and `uncovered` never enter the ok gate.
- Core `summary` keys: `{file, mutants, killed, survived, timeout, run_errors,
  kill_rate, budget, jobs, suspect, invalid_mutants, adaptive_timeout_s}`.
- Diff / prefilter / selection keys: `retry_total`, `executed`,
  `untouched_ranges`, `diff_skipped`, `total_sites`, `attention`,
  `coverage_prefilter`, `selection_mode`, `selection_groups`, `test_selection`,
  `attribution`, `zero_ref_short_circuit`.
- `failures` lists only non-killed records (each with an `id`), verdicts in
  survived / timeout / run_error / suspect / invalid_mutant / timeout_pinned /
  uncovered. Verdict and kind are open enums.
- exit `0` = pass; `1` = verdict fail / abort; `2` = argument error.

## Failure semantics

- **Abort shape**: baseline timeout or untrustworthy →
  `summary.{file, run_error: true, error}` with no killed keys; check
  `summary.run_error` before reading metrics.
- **Kill = newly-failing test-name set relative to baseline**, never the GUT
  exit code — a baseline with pre-existing failures would otherwise fake
  kills.
- `suspect` = dirty baseline with an empty difference set; `timeout_pinned` =
  timeout in 2+ consecutive runs (always re-run, visible, outside the gate);
  `uncovered` = the coverage prefilter judged the line uncovered (a quality
  signal: not executed, no budget consumed).
- Selection fail-loud (`MutationSelectionError`) → exit 1, stating why the
  mapping is unusable.

## Boundaries

- The real repo is never written: mutants land only in disk shadow slots
  (fresh ext4 farm per run on Linux, a persistent warm pool on win64); a
  four-level beacon proves the engine actually executed the mutant.
- Differential resume is on by default: unchanged scopes with
  killed/suspect/invalid verdicts are reused; survived/timeout/run_error sites
  always re-run (red line); `--mutate-all` forces full execution.
- `# gqt: no-mutate` filters sites (on or directly above a top-level `func` =
  function-level, anywhere else = line-level).
- Single-site reruns (`--mutation` / `--lines`) bypass diff reuse and the
  prefilter — the fair spot-check channel against false negatives.

## Command reference (aligned with v0.3.0 `--help`)

`gqt mutation <files...> --project-root <root> [all options]`

| Argument | Default | Meaning |
|---|---|---|
| `<files...>` (positional) | required, one or more | The `.gd` file(s) to mutate |
| `--project-root <root>` | required | Project root containing `addons/gut/` |
| `--budget <n>` | 50 | Max mutants; with diff reuse present, counted on the post-diff retry set (order-preserving truncation) |
| `--timeout <s>` | 60 | Single GUT wall-clock limit |
| `--dry-run` | off | List mutation sites without running GUT; emits a diff estimate when a manifest exists |
| `--tests <res://glob>` | omitted = truth-map selection | Explicit test subset that bypasses the mapping entirely; omitted = runtime truth-map selection (zero-ref terminal, unusable map fails loud) |
| `--jobs <n>` | `max(2, cpu//2)` | Worker pool size for per-file AND per-mutant slicing; `--jobs 1` forces serial; sites ≤ jobs falls back to serial automatically |
| `--resume` | off | Skip mutants whose verdicts are already in the resume store; fingerprint drift re-runs them |
| `--select {fn,file,full}` | `fn` | Selection granularity: fn (group by enclosing function), file (per-file set, legacy/calibration), full (explicit full-face audit, bypasses attribution) |
| `--attribution-map <path>` | auto-managed `.qa-cache/attribution-map.json` | Explicit `test-attribution/5` artifact; read-only pass-through, schema mismatch fails loud without rebuild |
| `--no-attribution-auto` | off | Disable automatic map generation/incremental refresh; with no usable map the run fails loud |
| `--mutation-warning <n>` | 50 | Warn on stderr + `summary.attention` when the pre-truncation site count exceeds N |
| `--timeout-factor <f>` | 2.0 | Adaptive per-mutant timeout = `max(timeout, baseline×f+1)`; only widened, never tightened |
| `--mutate-all` | off | Force full execution (the only escape from diff reuse) |
| `--mutation <file>#<scope_id>#<n>` | omitted | Single-site rerun (single file): 1 baseline + 1 mutant, bypasses diff reuse |
| `--lines <n>` | omitted | Single-site rerun by source line number (single file) |
| `--reuse-coverage` | same as default | Compatibility flag: the coverage prefilter is on by default; this flag is a no-op synonym |
| `--no-reuse-coverage` | off | Explicitly disable the coverage prefilter (effective only with a fresh store and a fully matching identity pack; drift disables it with a disclosure) |
| `--worker-channel` | on by routing | Force the resident worker; static routing sends autoload/GUT-domain/scene-held targets cold; `GQT_MUTATION_WORKER=0` opts out everywhere |
| `--godot-bin <path>` | omitted | Per-command Godot binary override; priority flag > env > PATH |
