# measure — measurement chain (suite / run / diagnose / map-update)

## When to use

- Build a reproducible **measurement suite definition** (which GUT face and
  which acceptance features participate in measurement).
- Run dual-arm coverage measurement: full GUT face plus acceptance suite, both
  instrumented, yielding per-function line-set unions and CRAP data.
- Diagnose per-feature acceptance timing.
- Incrementally rebuild the attribution map (`test-attribution/5`) that mutation
  selection depends on.

## Prerequisites

- `.gutconfig.json` and `acceptance/features/**/*.feature` exist (suite); GUT
  plus a Godot binary for `run` / `diagnose` / `map-update`.
- Measurement artifacts are exploratory / diagnostic-only (`not_certified`),
  not a certification gate or CI KPI.

## Typical examples

```bash
# 1) Build the suite definition (read-only, measurement-suite/2;
#    default output ./recipe.json)
gqt measure suite --project-root . --out recipe.json
# Exclusions require a reason (an empty reason is an invalid recipe)
gqt measure suite --project-root . --out recipe.json \
  --exclude 'acceptance_feature:some_feature:reason here'

# 2) Dual-arm batch measurement (measurement-report/2)
gqt measure run autoload systems --project-root . --recipe recipe.json --out report.json

# Common increments: parallel shards / acceptance repeat-union /
# stability baseline / evidence bundles
gqt measure run autoload systems --project-root . --recipe recipe.json --out report.json \
  --shard-width 6 --acceptance-repeats 3 \
  --stability-baseline previous-report.json --evidence-dir coverage-evidence

# Produce the attribution map (the truth source behind mutation selection)
gqt measure run autoload systems --project-root . --recipe recipe.json --out report.json \
  --test-attribution --test-attribution-out attribution-map.json

# 3) Per-feature timing diagnostics
gqt measure diagnose autoload systems --project-root . --recipe recipe.json \
  --features feature_a,feature_b --out diagnose.json

# 4) Incremental attribution rebuild (roots must match the baseline universe)
gqt measure map-update autoload systems --project-root . --recipe recipe.json \
  --baseline attribution-map.json --changed systems/example.gd \
  --out attribution-map.v2.json
```

## Output & exit codes

- `tool` is `measure.suite` / `measure.run` / `measure.diagnose` /
  `measure.map-update` respectively.
- suite: `measurement-suite/2`, with `gut_face`, `acceptance_face`,
  `toolchain`, `exclusions`, `recipe_hash` (identical content hashes
  identically across paths).
- run: `measurement-report/2`, with `crap` records (see crap.md) and the
  two-arm coverage union; `certification.status="not_certified"`,
  `evidence_level="exploratory"`, `diagnostic_only=true`.
- map-update: a `test-attribution/5` artifact; `--calibration-every`
  (default 5) forces a full rebuild after N increments.
- exit `0` = complete execution; `1` = verdict/validation failure;
  `2` = argument error. On diagnostic lanes `ok` only means the command
  completed; lane anomalies are data, not exit codes.

## Failure semantics

- Exclusion without a reason → invalid recipe (`ok=false`).
- Union validity: both arms must share source sha256, probe manifest and
  recipe_hash, differ in `run_uuid`, both be valid, and have different arm
  names; any violation → `valid=false` and `union=null`, so that file gets no
  score.
- Coverage across arms is a **line-set union, never an average**
  (`coverage = |C_gut ∪ C_acceptance| / total`).
- Incremental map rebuilds must pass a conservation check (dropped edges are
  rejected); unstable lanes produce no scores.

## Boundaries

- `measure run` temporarily instruments the target tree and restores it
  atomically in `finally`; no residue on production trees.
- The W4 probe (default `aidomain_integration`) records measured wall clock
  into `performance.acceptance_probe`; `measure risk --performance-file`
  accepts externally fed measurements.
- The attribution map is the single truth source for mutation selection;
  `measure coverage` line facts feed both the mutation coverage prefilter and
  the CRAP coverage input.

## Command reference (aligned with v0.3.0 `--help`)

### measure suite

`gqt measure suite --project-root <root> [--out recipe.json] [--exclude scope:entry:reason ...]`

| Argument | Default | Meaning |
|---|---|---|
| `--project-root <root>` | required | Target Godot project root |
| `--out <path>` | `./recipe.json` | Recipe JSON output path |
| `--exclude <scope:entry:reason>` | repeatable | Exclude a GUT dir or acceptance feature; an empty reason fails loud |

### measure run

`gqt measure run <roots...> --project-root <root> --recipe <recipe.json> --out <report.json> [all options]`

| Argument | Default | Meaning |
|---|---|---|
| `<roots...>` (positional) | required | Target `.gd` roots (recursive glob); arms run the full gutconfig face + acceptance suite over them |
| `--project-root <root>` | required | Project root containing `addons/gut` + `.gutconfig.json` |
| `--recipe <path>` | required | `measurement-suite/2` JSON from `gqt measure suite` |
| `--out <path>` | required | `measurement-report/2` output JSON path |
| `--baseline-failures <path>` | omitted | JSON of pre-existing un-instrumented failing `(script, test)` pairs; new failing pairs gate the arm; bare names are rejected as ambiguous |
| `--intolerant-tests <path>` | omitted | JSON declaring instrumentation-intolerant tests and coverage limitations; declared tests bypass failure gating by name and are disclosed as coverage blind spots |
| `--acceptance-probe-keys [keys]` | `aidomain_integration` | W4 acceptance cost-probe keys |
| `--timeout <s>` | 180 | GUT timeout seconds per arm |
| `--stability-baseline <path>` | omitted | Prior measurement report; fids whose covered lines differ route to the unstable lane (no score) |
| `--shard-width <n>` | 6 | gut_full parallel shard count (1 = sequential) |
| `--acceptance-repeats <n>` | 3 | Acceptance arm runs each shard group N times and unions the line sets; convergence round disclosed |
| `--acceptance-shards <n>` | 4 | Acceptance parallel shard count |
| `--shard-log-dir <dir>` | omitted | Retain per-shard full logs and raw sink copies |
| `--evidence-dir <dir>` | omitted | Per-arm `coverage-evidence/2` bundle + run record output dir |
| `--test-attribution` | off | Emit a test→file attribution artifact (`test-attribution/5`); with it off, the default path is byte-identical |
| `--test-attribution-out <path>` | `<--out>.attribution.json` | Attribution artifact path |
| `--attribution-causal` | off | Also wrap `call_deferred/create_timer` creation sites so their callbacks are attributed to the creating test; requires `--test-attribution` |
| `--acceptance-adaptive` | off | Run acceptance reps round-by-round and stop early once the line-set union stabilises (bound stays `--acceptance-repeats`) |
| `--arms-parallel` | off | Run gut_full and acceptance arms concurrently; the timing baseline must be redefined when enabled |
| `--godot-bin <path>` | omitted | Per-command Godot binary override |

### measure diagnose

`gqt measure diagnose <roots...> --project-root <root> --recipe <recipe.json> --features <k1,k2> --out <diagnose.json> [--timeout 480] [--godot-bin <path>]`

| Argument | Default | Meaning |
|---|---|---|
| `<roots...>` (positional) | required | Directories whose `.gd` files are instrumented |
| `--project-root <root>` | required | Target Godot project root |
| `--recipe <path>` | required | Measurement suite JSON (verify + feature lookup) |
| `--features <k1,k2>` | required | Comma-separated acceptance feature keys |
| `--out <path>` | required | Diagnostic report JSON path (progress written after each feature) |
| `--timeout <s>` | 480 | Per-leg GUT timeout seconds |
| `--godot-bin <path>` | omitted | Per-command Godot binary override |

### measure map-update

`gqt measure map-update <roots...> --project-root <root> --recipe <recipe.json> --baseline <map.json> [--changed <path>] --out <map.json> [--timeout 180] [--calibration-every 5] [--attribution-causal] [--godot-bin <path>]`

| Argument | Default | Meaning |
|---|---|---|
| `<roots...>` (positional) | required | Target `.gd` roots — must be the same universe as the baseline artifact |
| `--project-root <root>` | required | Target Godot project root |
| `--recipe <path>` | required | `measurement-suite/2` JSON (same content as the baseline run) |
| `--baseline <path>` | required | Prior attribution artifact (`test-attribution/5`) |
| `--changed <path>` | omitted, repeatable | Changed/new/removed file path |
| `--out <path>` | required | Incremented attribution artifact output path |
| `--timeout <s>` | 180 | Per-leg GUT timeout seconds |
| `--calibration-every <n>` | 5 | Force a full-face rebuild after N increments |
| `--attribution-causal` | off | Causal attribution lane |
| `--godot-bin <path>` | omitted | Per-command Godot binary override |
