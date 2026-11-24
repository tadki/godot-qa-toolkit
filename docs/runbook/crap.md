# CRAP — code risk scoring

## When to use

Use CRAP (Change Risk Anti-Patterns) to decide **which functions to refactor
first**: highly complex and under-tested functions carry the most risk. CRAP is
a **risk-ranking and governance signal**, not a new silent CI gate — do not bury
it as a pass/fail judgment inside a pipeline.

## Formula (Uncle Bob standard)

```text
CRAP(cc, c) = cc² × (1 − c)³ + cc
```

- `cc`: the function's cyclomatic complexity.
- `c`: the function's line coverage as a **raw 0–1 ratio** — never pre-rounded
  to a percentage, because a rounded 100% would silently zero the penalty term.
- `c = 1` (100% coverage) → `score = cc` (only the complexity term remains).
- No valid coverage denominator (`coverage=None` / vacuous file) → **no score**
  (`None`); never substitute 0 or any default to fabricate a score.
- Out-of-range or NaN coverage → explicit error (`ValueError`), never clamped
  into a legal-looking score.

## Risk thresholds

| Criterion | Meaning |
|---|---|
| `score ≥ 30` or `cc ≥ 15` | CRAP / high risk: refactor this function first (add tests to lower `c`, then split to lower `cc`) |
| everything else | Sort descending by score; higher scores come first |

Thresholds follow Uncle Bob's crap4java defaults; do not invent others.

## Typical examples

```bash
# Function-level CRAP from a measurement report (acceptance is paused by
# default; restored dual-arm reports union coverage from both arms)
gqt measure run autoload systems --project-root . --recipe recipe.json --out report.json
jq '.crap.summary' report.json
jq '.crap.records[] | {function_id, cc, coverage, score: .crap_line.score, score_status}' report.json

# CRAP-Line shadow over a few candidate files (no whole-repo extrapolation)
gqt measure risk src/systems/example.gd --project-root . --report shadow-report.json

# Static dry run: complexity side only, no GUT rounds
gqt measure risk src/systems/example.gd --project-root . --skip-measure --report shadow-report.json
```

## Output & exit codes

- run report: each `crap.records[]` entry carries `function_id`, `cc`,
  `covered`, `total`, `coverage`, `score_status`, `coverage_semantics`,
  `crap_line{score, …}`; `crap.summary` carries `functions`, `scored`, `lanes`.
- risk report: `shadow/2` + `crap-line/1`, a score-descending ranking signal.
- `coverage_semantics`: `statement_start_line` = exact per-statement start
  lines; `block_entry_approximation` = block-entry hits expanded to in-block
  statements — never treat the approximation as exact per-statement data.
- exit `0` = command completed; `1` = verdict/execution failure; `2` = CLI
  usage error. On diagnostic lanes `ok` does not mean "risk-free"; conclusions
  come from scores and ranking only.

## Failure semantics

- `score_status` other than `scored` (no denominator / unstable lane /
  unmeasured) → `score=None`: excluded from ranking, never back-filled with 0.
- Unmeasurable files (lambda / inner-class / var-def shapes) route to
  measurement-gap lanes, disclosed as such, with no fabricated scores.
- Illegal coverage (out of range / NaN) → explicit error, no fabricated score.
- An explicit coverage `--tests` glob that selects zero scripts fails with
  `empty selection face` and exit `1`; it never emits an authoritative-looking
  `0.0%`. A glob that matches scripts whose covered-line set is empty remains
  a genuine zero-coverage result.
- A target outside the pre-existing map's universe fails before any GUT leg
  with `target_not_in_map_universe` and rebuild guidance; it does not burn an
  instrumented arm before reporting the mismatch.
- A non-`.gd` or malformed GDScript coverage target and an unparseable risk
  `--performance-file` return `ok=false` with exit `1`. The flat JSON envelope
  keeps `failures[]` present and names `error` / `reason` / `reason_detail` /
  `suggested_action` in `summary`; no Python traceback leaks to stdout or
  stderr.

## Boundaries and input faces

- **CRAP inputs are not standalone quality verdicts**:
  - `gqt measure coverage <file>` supplies the line-coverage facts behind `c`,
    with sentinel semantics preserved — `probes_fired: 0` (probes never fired)
    and `vacuous` (zero executable lines) are **not** "true 0% coverage"; treat
    them as run_error / vacuous, not as a coverage conclusion. `elif`
    condition lines belong to the coverage and attribution surface: executing
    the branch body records the condition as covered (the true path), while a
    false-path evaluation alone does not count it. `else` header lines remain
    excluded because they have no condition-evaluation semantics.
  - `gqt measure complexity <paths>` supplies function-level `cc` (its
    `--warn`/`--max` flags are that command's own threshold surface); for CRAP,
    consume the measured `cc` directly instead of setting a separate verdict.
- Results from 2–3 files must not be extrapolated into whole-repo conclusions;
  CRAP ranks candidates, and the final refactor decision stays with a human.

## Command reference (aligned with v0.4.1 `--help`)

### measure risk

`gqt measure risk <files...> [--project-root <root>] [--core-tests D] [--full-tests D] [--report shadow-report.json] [--performance-file F] [--skip-measure] [--godot-bin <path>]`

| Argument | Default | Meaning |
|---|---|---|
| `<files...>` (positional) | required | Candidate `.gd` files (eligible ones are measured; lambda/inner-class/var-def files route to measurement-gap lanes) |
| `--project-root <root>` | discover from candidate files/CWD | Target Godot project root; explicit value wins and invalid values fail loud |
| `--core-tests <dir>` | `res://tests/unit/` | GUT dir for the core recipe |
| `--full-tests <dir>` | `res://tests/` (via `.gutconfig` dirs) | GUT dir for the full recipe |
| `--report <path>` | `./shadow-report.json` | Shadow JSON output path |
| `--performance-file <path>` | omitted | Optional JSON with measured complexity/coverage head-vs-master medians; Gherkin timing uses `gherkin_cost`, while retired `acceptance_cost` is rejected rather than silently dropped |
| `--skip-measure` | off | Static-only dry run: no GUT rounds executed |
| `--godot-bin <path>` | omitted | Per-command Godot binary override |

### measure coverage (CRAP coverage input)

`gqt measure coverage [file] [--project-root <root>] [--min-percent 80] [--timeout 120] [--tests D] [--evidence-out D] [--prune-sinks] [--retain-days 7] [--godot-bin <path>]`

| Argument | Default | Meaning |
|---|---|---|
| `file` (positional) | required except `--prune-sinks` | The `.gd` file to measure from the line-level map |
| `--project-root <root>` | discover from target/CWD | Project root containing `addons/gut/`; explicit value wins |
| `--min-percent <p>` | 80 | Minimum line coverage percent |
| `--timeout <s>` | 120 | Timeout for map build / incremental refresh; a fresh projection starts no process |
| `--tests <res://dir-or-.gd>` | omitted = full mapped test face | Restrict mapped test scripts by directory boundary or exact file; zero matching scripts fail as `empty selection face`, never as a silent `0.0%` |
| `--evidence-out <dir>` | omitted | Write a `coverage-evidence/2` bundle from the line map; no second GUT run |
| `--prune-sinks` | off | Delete `qa-coverage-hits-*` sinks older than `--retain-days` under `user://` and project root |
| `--retain-days <n>` | 7 | Retention period for `--prune-sinks` |
| `--godot-bin <path>` | omitted | Per-command Godot binary override |

### measure complexity (CRAP complexity input)

`gqt measure complexity <paths...> [--warn 10] [--max 15]`

| Argument | Default | Meaning |
|---|---|---|
| `<paths...>` (positional) | required | One or more `.gd` files or directories to scan |
| `--warn <n>` | 10 | Warn threshold (`warn < cc ≤ max` records a warning, not a failure) |
| `--max <n>` | 15 | Fail threshold (`cc > max` records a violation); with `--max < --warn`, warn merges into max |
