# measure — measurement chain (suite / run / diagnose / map-update)

## When to use

- Build a reproducible **measurement suite definition** (which GUT face and
  which acceptance features participate in measurement).
- Run dual-arm coverage measurement: full GUT face plus acceptance suite, both
  instrumented, yielding per-function line-set unions and CRAP data. With
  `--test-attribution`, the same run also records per-test/per-script covered
  lines as the primary attribution facts.
- Diagnose per-feature acceptance timing.
- Incrementally rebuild the line-primary attribution map (`test-attribution/6`)
  that mutation selection and Coverage Store projection depend on.

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

# Produce the line-primary attribution map (the truth source behind mutation).
# The same measured line facts later project mutation's Coverage Store without
# another instrumentation or GUT pass.
gqt measure run autoload systems --project-root . --recipe recipe.json --out report.json \
  --test-attribution --test-attribution-out attribution-map.json

# 3) Per-feature timing diagnostics
gqt measure diagnose autoload systems --project-root . --recipe recipe.json \
  --features feature_a,feature_b --out diagnose.json

# 4) Incremental attribution rebuild (roots must match the baseline universe)
# --changed accepts changed/new/removed paths. Removing a production file with
# baseline edges is `prod_removed`: its dead edges are purged and tests that
# touched it plus depth-1 static consumers are rerun.
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
- run exit semantics: `summary` adds `certification` (`certified` /
  `uncertified` / `unusable`), `union_certified`, `attribution_usable`,
  `exit_semantics` (`certified` / `artifacts_usable_union_uncertified` /
  `failed`) and `attribution_map` when an attribution artifact exists. A union
  that cannot be certified while the attribution map is fresh this run and its
  arms carry execution evidence exits **0** with `certification=uncertified` —
  do not discard the usable artifact. Without a usable map (stale map files do
  not count) it still exits 1 with `reason="union invalid or arms invalid"`.
- map-update: a `test-attribution/6` artifact; changed production files replace
  their related line facts, changed tests replace the whole test identity, and
  file/function/reverse views are recomputed from the merged lines. A deleted
  production file with baseline edges is `prod_removed`: its line facts and
  derived edges are purged, tests that touched it plus depth-1 static consumers
  are rerun, and its target/file-hash/instrumentable entries are removed.
  Non-`.gd` paths and deleted files without baseline edges are `unknown` and
  rejected by name (`IncrementError`); they never become a silent zero-rerun
  disclosure bucket. `--calibration-every` (default 5) forces a full rebuild
  after N increments.
- exit `0` = complete execution (including `measure run` with a usable
  attribution map whose union is uncertified); `1` = verdict/validation
  failure; `2` = argument error. On diagnostic lanes `ok` only means the
  command completed; lane anomalies are data, not exit codes.

## Failure semantics

- Exclusion without a reason → invalid recipe (`ok=false`).
- Union validity: both arms must share source sha256, probe manifest and
  recipe_hash, differ in `run_uuid`, both be valid, and have different arm
  names; any violation → `valid=false` and `union=null`, so that file gets no
  score.
- Coverage across arms is a **line-set union, never an average**
  (`coverage = |C_gut ∪ C_acceptance| / total`).
- Incremental map rebuilds must pass a conservation check (dropped identities
  outside the declared rerun/delete surface are rejected); unstable lanes
  produce no scores.
- Attribution usability requires real execution evidence: no bundle, zero tests,
  or zero probes without `vacuous` is invalid. A legitimate empty face remains
  usable when probes fired or the measured surface is genuinely vacuous. An
  invalid map fails loud; auto mode rebuilds instead of consuming a false empty
  truth.

## Boundaries

- `measure run` temporarily instruments the target tree and restores it
  atomically in `finally`; no residue on production trees.
- The acceptance arm runs **shared shards** (SEE-1619): features are grouped
  into private suites so several keys share one GUT process, with real fan-out
  `len(groups) × reps` instead of one process per feature per rep. A feature
  only falls back to a singleton process when it fails shared validation this
  run; such downgrades are disclosed by name in `process_fallbacks:[{key,reason}]`
  (sorted by key, present on every return path, empty when fully atomic), never
  packaged as valid when both shared and isolated runs are red, and their
  concurrency is still capped by `--acceptance-shards`. Cleanup failure marks
  the arm invalid and discloses the residue path.
- The W4 probe (default `aidomain_integration`) records measured wall clock
  into `performance.acceptance_probe`; `measure risk --performance-file`
  accepts externally fed measurements.
- The `/6` attribution map stores `test_to_lines`, `script_to_lines`,
  `line_to_tests`, and the instrumentable line surface. File/function views are
  derived from those lines plus the source registry, not independently measured.
- Mutation projects the scripts in its actual `tests_glob` into a
  `see1420-coverage/2` entry from the same `/6` facts. A persisted projection —
  bound to the current map's content and execution evidence (SEE-1594 A05) —
  or a fresh map can serve the prefilter without another instrumentation pass;
  binding drift invalidates the projection and is disclosed.
- The manual `see1420-coverage/1` store is retired (SEE-1594 A04): mutation
  prefilter no longer reads it, and no writer produces it. Leftover `/1`
  files on disk are rejected by schema. Coverage answers come from the
  line-level map (single source of truth).
- Schema upgrades are fail-loud. Auto-managed old or unusable maps are rebuilt;
  an explicit old-schema `--attribution-map` is rejected without being rewritten.
- The internal `DEFAULT_RERUN_FULL_FACE_RATIO` is `0.05`: if the candidate
  rerun scripts exceed 5% of the baseline script face, map-update falls back to
  a full-face rebuild. This was calibrated on a warm KOL copy (824 files / 285
  scripts): incremental cost was approximately `16.16s + 8.878s × rerun
  scripts`, full-face cost was `223–227s`, and the crossover was about 23.6
  scripts (8.3%). The 5% cap estimates about 140.5s at the boundary, below the
  225.25s full-face midpoint and with noise-band headroom.
- Each arm carries `unattributed_files`: production files with unattributed
  hits, for which test-window attribution is lossy. Incremental artifacts carry
  the union of the baseline list and this round's changed/new production files.
  Mutation's test-level filter treats a listed file (or a missing key) as a
  conservative whole-file allow scope instead of trusting line/fn precision.

## Acceptance performance harness (SEE-1619)

`tests/integration/run_see1619_acceptance_perf.py` compares the **shared-shard
and per-feature process shapes within the same gqt branch** (not a cross-version
master comparison) on the same KOL input, reps, concurrency and an isolated
store, never extrapolating wall clock from process counts:

```bash
PYTHONPATH=src python3 -I tests/integration/run_see1619_acceptance_perf.py \
  --project-root <kol-copy> --godot "$GODOT" --aa 3 --rounds 3 \
  --report <work>/acceptance-perf.json
```

- A/A noise samples come only from the independent shared-A block before the
  first B (`baseline_samples ≥ 3`); interleaved A/B pairs (`interleaved_pairs
  ≥ 3`) must follow that block, and starting interleaving with an insufficient
  baseline is a named batch failure.
- `speed_gate.ok` requires `median_ratio > threshold` **strictly** — equality
  is not a pass; reports carry absolute seconds, ratios and per-round
  process/shard accounts, and INCONCLUSIVE is never written as PASS.
- Batch rounds (`--mode A/B --round-index`) cannot bypass the merged summary:
  once `rounds_complete=true`, the exit code follows that summary (MEASURED →
  0, otherwise 1); incomplete reports resume under the same semantics.
- Disclosure limit (LOW D-A): the summary does not carry the underlying
  `rc/exit_semantics` or gut-arm health; a single round's `rc=1/ok=false` is
  uncertified-by-design, and MEASURED only proves acceptance-arm validity plus
  round completeness plus the speed gate.

## Command reference (aligned with v0.4.0 `--help`)

### measure suite

`gqt measure suite [--project-root <root>] [--out recipe.json] [--exclude scope:entry:reason ...]`

| Argument | Default | Meaning |
|---|---|---|
| `--project-root <root>` | discover from CWD | Target Godot project root; explicit value wins and invalid values fail loud |
| `--out <path>` | `./recipe.json` | Recipe JSON output path |
| `--exclude <scope:entry:reason>` | repeatable | Exclude a GUT dir or acceptance feature; an empty reason fails loud |

### measure run

`gqt measure run <roots...> [--project-root <root>] --recipe <recipe.json> --out <report.json> [all options]`

| Argument | Default | Meaning |
|---|---|---|
| `<roots...>` (positional) | required | Target `.gd` roots (recursive glob); arms run the full gutconfig face + acceptance suite over them |
| `--project-root <root>` | discover from roots/CWD | Project root containing `addons/gut` + `.gutconfig.json`; explicit value wins |
| `--recipe <path>` | required | `measurement-suite/2` JSON from `gqt measure suite` |
| `--out <path>` | required | `measurement-report/2` output JSON path |
| `--baseline-failures <path>` | omitted | JSON of pre-existing un-instrumented failing `(script, test)` pairs; new failing pairs gate the arm; bare names are rejected as ambiguous |
|  |  | Without it, the gut_full arm **lazily** acquires the baseline only when the instrumented arm observed failures (one same-face uninstrumented GUT run; green arms pay zero extra GUT); acquisition failure is fail-loud and the arm stays invalid. With `--test-attribution`, the emitted attribution map records a `baseline_failures_meta` disclosure key (`method` / `face` / `count` / `sha256` / `wall_s`) describing how that baseline was acquired; the key is carried across `measure map-update` increments and is disclosure-only (consumers do not read it). The acceptance arm is never baselined — its failures stay visible |
| `--intolerant-tests <path>` | omitted | JSON declaring instrumentation-intolerant tests and coverage limitations; declared tests bypass failure gating by name and are disclosed as coverage blind spots |
| `--acceptance-probe-keys [keys]` | `aidomain_integration` | W4 acceptance cost-probe keys |
| `--timeout <s>` | 180 | GUT timeout seconds per arm |
| `--stability-baseline <path>` | omitted | Prior measurement report; fids whose covered lines differ route to the unstable lane (no score) |
| `--shard-width <n>` | 6 | gut_full parallel shard count (1 = sequential) |
| `--acceptance-repeats <n>` | 3 | Acceptance arm runs each shard group N times and unions the line sets; convergence round disclosed |
| `--acceptance-shards <n>` | 4 | Acceptance parallel shard count |
| `--shard-log-dir <dir>` | omitted | Retain per-shard full logs and raw sink copies |
| `--evidence-dir <dir>` | omitted | Per-arm `coverage-evidence/2` bundle + run record output dir |
| `--test-attribution` | off | Emit a line-primary `test-attribution/6` artifact with per-test/per-script covered lines, reverse lookup, derived file/function views and execution evidence; with it off, the default path is byte-identical |
| `--test-attribution-out <path>` | `<--out>.attribution.json` | Attribution artifact path |
| `--attribution-causal` | off | Also wrap `call_deferred/create_timer` creation sites so their callbacks are attributed to the creating test; requires `--test-attribution` |
| `--acceptance-adaptive` | off | Run acceptance reps round-by-round and stop early once the line-set union stabilises (bound stays `--acceptance-repeats`) |
| `--arms-parallel` | off | Run gut_full and acceptance arms concurrently; the timing baseline must be redefined when enabled |
| `--godot-bin <path>` | omitted | Per-command Godot binary override |

### Watching progress (`measure run`)

`measure run` writes progress lines to **stderr** only (stdout stays exactly one
JSON object); they are disclosure-only and never affect arm validity or the
`ok` gate:

```text
measure: arm=gut_full start shards=6 reps=1
measure: arm=gut_full shard=3/6 rep=0/1 start
measure: arm=gut_full shard=3/6 done s=42.1
measure: arm=gut_full done state=valid wall_s=180.3
measure: arm=acceptance start shards=4 reps=3
measure: lazy baseline 2 failing pairs acquired uninstrumented (75.7s) — only NEW failures gate the arm
```

For per-shard raw GUT logs, pass `--shard-log-dir <dir>` and follow the files:
`tail -f <dir>/gut_full-shard<i>-rep<r>.log` (one file per shard/repetition).

### measure diagnose

`gqt measure diagnose <roots...> [--project-root <root>] --recipe <recipe.json> --features <k1,k2> --out <diagnose.json> [--timeout 480] [--godot-bin <path>]`

| Argument | Default | Meaning |
|---|---|---|
| `<roots...>` (positional) | required | Directories whose `.gd` files are instrumented |
| `--project-root <root>` | discover from CWD | Target Godot project root; explicit value wins and invalid values fail loud |
| `--recipe <path>` | required | Measurement suite JSON (verify + feature lookup) |
| `--features <k1,k2>` | required | Comma-separated acceptance feature keys |
| `--out <path>` | required | Diagnostic report JSON path (progress written after each feature) |
| `--timeout <s>` | 480 | Per-leg GUT timeout seconds |
| `--godot-bin <path>` | omitted | Per-command Godot binary override |

### measure map-update

`gqt measure map-update <roots...> [--project-root <root>] --recipe <recipe.json> --baseline <map.json> [--changed <path>] --out <map.json> [--timeout 180] [--calibration-every 5] [--attribution-causal] [--godot-bin <path>]`

| Argument | Default | Meaning |
|---|---|---|
| `<roots...>` (positional) | required | Target `.gd` roots — must be the same universe as the baseline artifact |
| `--project-root <root>` | discover from CWD | Target Godot project root; explicit value wins and invalid values fail loud |
| `--recipe <path>` | required | `measurement-suite/2` JSON (same content as the baseline run) |
| `--baseline <path>` | required | Prior line-primary attribution artifact (`test-attribution/6`) |
| `--changed <path>` | omitted, repeatable | Changed/new/removed file path |
| `--out <path>` | required | Incremented attribution artifact output path |
| `--timeout <s>` | 180 | Per-leg GUT timeout seconds |
| `--calibration-every <n>` | 5 | Force a full-face rebuild after N increments |
| `--attribution-causal` | off | Causal attribution lane |
| `--godot-bin <path>` | omitted | Per-command Godot binary override |
