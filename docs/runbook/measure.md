# measure — measurement chain (suite / scope / run / diagnose / map-update)

## When to use

- Build a reproducible **measurement suite definition** (which GUT face and
  which acceptance features participate in measurement).
- Run coverage measurement over the full GUT face, yielding per-function
  line coverage and CRAP data. Acceptance is paused by default; when explicitly
  restored, active arms' line sets are unioned into the same report. With
  `--test-attribution`, the run also records per-test/per-script covered lines
  as the primary attribution facts.
- Print the project's scope resolution — which files count as production,
  non-production, tests, and non-tests under the `.gqt-scope.json` rules
  (`measure scope`, read-only, no GUT).
- Diagnose per-feature acceptance timing.
- Refresh the project's single test-attribution map (`test-attribution/7`),
  the line-primary fact store behind mutation verdicts, selection and the
  Coverage Store projection. `measure map-update` creates, increments, or
  fully rebuilds it, or reports that it is already fresh.

## Plan-first flow

`gqt measure run` follows the same `plan → prepare → run` shape as
`gqt mutation`:

1. **plan** — recipe build, target universe enumeration, exclusion
   validation. Output is the in-memory suite, no execution.
2. **prepare** — attribution instrumentation of the target tree, attribution
   map build/refresh, **baseline preflight**, warm pool provisioning. Stage
   timings are disclosed on the report as
   `stage_timings.{instrument_prepare, baseline_preflight, gut_full_arm,
   gherkin_arm, planning, total}` (seconds, summed across the matching
   stage if it fires more than once). Each stage carries its own wall clock;
   a missing stage means it did not run.
3. **run** — the active instrumented arm or arms consume the prepared map.
   The report is written only after every named failure has been classified.

**Baseline preflight** runs the uninstrumented baseline GUT once before any
instrumented arm. When it observes failing `(script, test)` pairs:

- default: the red tests are **excluded by name** from the instrumented
  arms and disclosed on the map as `status=partial` plus
  `excluded_tests=[(script, test), ...]`; the run continues. Print line:
  `measure: baseline_preflight RED — N failing pair(s) ... EXCLUDED by name
  ... mapping marked partial, run continues`.
- explicit short-stop: set `GQT_MEASUREMENT_FAIL_ON_RED_BASELINE=1` (or any
  non-empty / non-`0` / non-`false` value) to fail loud with
  `MeasurementError("baseline_preflight_red: ... top-N of M failing tests")`
  instead. With this switch the instrumented arms do not run.

The preflight result is reused for the instrumented arms (the lazy-baseline
path is skipped when preflight has already produced the failing-pair set).

**Acceptance arm is paused by default** (Owner decision in SEE-1615
§SPEC-PLANFIRST). This applies equally to ordinary runs and
`--test-attribution` runs. The acceptance arm has `state=disabled`,
`stage_timings.gherkin_arm` is `0.0`, and the report field
`report.gherkin_arm = "disabled_pending_atomization — restored after SEE-1619
(shared-process shards); env override GQT_MEASUREMENT_GHERKIN_DISABLED=0"`
discloses the pause.

While paused, an ordinary run without `--test-attribution` uses single-arm
exit semantics: a valid, passing `gut_full` arm returns `ok=true` / exit `0`
with `certification=uncertified` and
`exit_semantics=single_arm_usable_gherkin_paused`; an invalid or failing
arm returns `ok=false` / exit `1`. Attribution runs retain their
`attribution_usable` semantics, and an explicitly restored dual-arm run keeps
the union semantics. To restore acceptance measurement, set
`GQT_MEASUREMENT_GHERKIN_DISABLED=0` after SEE-1619 has landed; the default
(unset or `1`) keeps the arm paused. Exact stripped `""`, `0`, or `false`
restores it, so a whitespace-only value also restores; other values keep it
paused. Any presence of the retired `GQT_MEASUREMENT_ACCEPTANCE_DISABLED` —
including `""`, whitespace, `0`, or `false` — is a named error, never a silent
alias.

**Target universe anchoring**: positional roots are anchored to the
project root via `anchor_to_project_root`. When the requested paths fall
outside the project root or sit at the project top level, the target
universe falls back to the project root and discloses this on stderr as
`measure: targets_root_fallback (<reason>) — target face widened to the
whole project root (<root>)`. The fallback is named, not silent.

## Prerequisites

- `.gutconfig.json` and `addons/gqt_gherkin/features/*.feature` exist (suite); GUT
  plus a Godot binary for `run` / `diagnose` / `map-update`. `measure suite`
  also requires the strict-root `harness.gd` and a nonempty addon
  `.gd`/`.feature` source set for its adapter fingerprint.
- Measurement artifacts are exploratory / diagnostic-only (`not_certified`),
  not a certification gate or CI KPI.

## Typical examples

```bash
# 1) Build the suite definition (read-only, measurement-suite/3;
#    default output ./recipe.json)
gqt measure suite --project-root . --out recipe.json
# Exclusions require a reason (an empty reason is an invalid recipe)
gqt measure suite --project-root . --out recipe.json \
  --exclude 'gherkin_feature:some_feature:reason here'

# 2) Measurement report (acceptance paused by default)
gqt measure run autoload systems --project-root . --recipe recipe.json --out report.json

# Common increments: parallel shards / stability baseline / evidence bundles
gqt measure run autoload systems --project-root . --recipe recipe.json --out report.json \
  --shard-width 6 --stability-baseline previous-report.json --evidence-dir coverage-evidence

# Produce the line-primary attribution map (the truth source behind mutation).
# The same measured line facts later project mutation's Coverage Store without
# another instrumentation or GUT pass. The artifact lands next to the report
# as <out>.attribution.json; its path is disclosed as summary.attribution_map.
gqt measure run autoload systems --project-root . --recipe recipe.json --out report.json \
  --test-attribution

# 3) Per-feature timing diagnostics
gqt measure diagnose autoload systems --project-root . --recipe recipe.json \
  --features feature_a,feature_b --out diagnose.json

# 4) Refresh the project's unique attribution map. No map paths are passed:
# the map lives at .qa-cache/attribution-map.json and the tool decides whether
# to create, increment, or fully rebuild it (or reports it already fresh).
gqt measure map-update --project-root .

# Print the scope resolution the map universe is built over (read-only)
gqt measure scope --project-root .
```

## Output & exit codes

- `tool` is `measure.suite` / `measure.scope` / `measure.run` /
  `measure.diagnose` / `measure.map-update` respectively.
- suite: `measurement-suite/3`, with `gut_face`, `gherkin_face`,
  `toolchain`, `exclusions`, `recipe_hash` (identical content hashes
  identically across paths). `gherkin_face` requires `features`, `count`,
  `excluded`, `harness`, `adapter_source_sha256`, and `adapter_source_files`;
  `harness` requires `worker="generated/gherkin_worker.gd"`, `ops_version`, and
  `wiring_sha256`. The adapter fingerprint recursively covers addon
  `.gd`/`.feature` sources (no-follow; `.uid`, README, and `.json` excluded).
  Legacy `/2`, `acceptance_face`, and `acceptance_feature` inputs fail loud;
  the exclusion scope is `gherkin_feature`.
- run: `measurement-report/3`, with `crap` records (see crap.md) and
  active-arm coverage data; a restored dual-arm run carries the two-arm
  coverage union. Every report remains
  `certification.status="not_certified"`, `evidence_level="exploratory"`,
  `diagnostic_only=true`.
- run exit semantics: `summary` adds `certification` (`certified` /
  `uncertified` / `unusable`), `union_certified`, `attribution_usable`,
  `exit_semantics` (`certified` / `artifacts_usable_union_uncertified` /
  `single_arm_usable_gherkin_paused` / `failed`) and `attribution_map`
  when an attribution artifact exists. A fresh, execution-backed attribution
  map with an uncertified union exits **0** as
  `artifacts_usable_union_uncertified`. With acceptance paused, an ordinary
  run without attribution instead exits **0** when `gut_full` is valid,
  disclosed as `single_arm_usable_gherkin_paused`. Outside those usable
  forms, an invalid union or arm exits **1** with
  `reason="union invalid or arms invalid"`; stale map files alone do not make
  an attribution run usable.
- scope: `summary` carries `rules_file_present`, `rules_sha256`,
  `defaults_used`, `unconfigured_hint`, `counts` (per class: `production`,
  `non_production`, `test`, `non_test`), `universe_count`, `gd_count`, the four
  full class lists (`production`, `non_production`, `test`, `non_test`), plus
  `per_dir_counts`, `gut_dirs_effective`, `gut_dirs_excluded`,
  `features_excluded`, `excluded_production`, `excluded_production_stats`, and
  `excluded_reference_warnings`. Without `.gqt-scope.json` the generic default
  applies (`defaults_used=true`) and `unconfigured_hint` names it. A missing
  project root is a named failure (`project root not found`, exit `1`).
- map-update: `summary.mode` is what the map's status predicted before the
  refresh — `create` / `increment` / `full` / `fresh` — with `map_path`,
  `already_fresh` and `fresh` alongside. `reason` appears only when a full
  rebuild was involved: it is the first structural factor that forced one (for
  example `recipe: … recipe_hash_pristine stale vs current tree — rebuild the
  map`) or, for a fallback decided inside the increment leg, the map ledger's
  `increment.reason` (one of `hash_mismatch_fallback`,
  `new_prod_file_fallback`, `calibration_due_fallback`,
  `rerun_surface_too_large_fallback`,
  `gherkin_rerun_surface_too_large_fallback`). A `fresh` response echoes the
  `reason` its ledger still carries. A failed refresh returns `ok=false` with
  `summary.mode` plus `summary.error` (exit `1`); the old map's bytes are left
  untouched.
- The refreshed artifact is `test-attribution/7`. Its `test_verdicts` table
  holds one entry per test identity — green entries are `{"verdict": "green"}`,
  red entries are `{"verdict": "red", "classification":
  "pre_existing" | "suspected_intolerant" | "new", "script": …}` — and
  `identity_collisions` discloses, per `basename_key`, how a red key that
  arrived under a bare script basename or without a script was handled:
  `same_basename_resolved_by_path` (one of the `candidates` matched the full
  script path and became `resolved`), `same_basename_no_path_match` (no path
  match — every candidate is written red), or
  `empty_script_red_key_disclosed_not_emitted` (a bare test-name echo with an
  empty script is disclosed only and never enters the table). Test identities
  are full `res://` script paths
  (`res://tests/unit/affix/test_affix_factory.gd::test_q3…`), never bare
  basenames; Gherkin identities read
  `gherkin://feature/<feature key>::<compiled test name>`, where the second
  segment is the compiled test name (`_ident(scenario, index)` plus the
  zero-padded case ordinal) — not the scenario name.
- After a create, increment, or full refresh the map is current and usable, so
  `measure map-update`, `measure coverage`, `measure risk`, and `mutation`
  consume it directly. An increment ledger (`increment`) records `mode`,
  `increments_since_full`, `calibration_policy`, `changed_files` (the
  test/production/content/feature change classes),
  `rerun_scripts`/`dropped_scripts`, `rerun_executions` and
  `gherkin_leg_executions`, plus `event_counts_stale` as a disclosure that the
  hit-event counters were not re-measured.
- A Gherkin increment leg's execution account carries `rc`, `shard`,
  `executed_scripts`, `failing_tests`, `failing_scenarios`, `out` and `error`.
  Verdicts are written back per identity from `failing_tests`, which lives in
  the same namespace as the identity's second segment (compiled test names):
  a scenario that ran and failed stays red, one that passed goes green, and the
  baseline classification is carried over. `failing_scenarios` holds scenario
  names and is execution-account disclosure only — it is never compared
  against identities. When the leg failed but no failing name can be
  attributed (`failing_tests` absent or empty), every identity of that feature
  key is written red.
- exit `0` = complete execution (including a usable attribution map whose
  union is uncertified, or a valid ordinary GUT-only run while acceptance is
  paused); `1` = verdict/validation failure; `2` = argument error. On
  diagnostic lanes `ok` only means the command completed; lane anomalies are
  data, not exit codes.

## Failure semantics

- Exclusion without a reason → invalid recipe (`ok=false`).
- Union validity on a restored dual-arm run: both arms must share source
  sha256, probe manifest and recipe_hash, differ in `run_uuid`, both be valid,
  and have different arm names; any violation → `valid=false` and
  `union=null`, so that file gets no score.
- Coverage across active arms is a **line-set union, never an average**
  (`coverage = |C_gut ∪ C_acceptance| / total` when both arms are active).
- Incremental map rebuilds must pass a conservation check (dropped identities
  outside the declared rerun/delete surface are rejected); unstable lanes
  produce no scores.
- Attribution usability requires real execution evidence: no bundle, zero tests,
  or zero probes without `vacuous` is invalid. A legitimate empty face remains
  usable when probes fired or the measured surface is genuinely vacuous. An
  invalid map fails loud; the next preparation renews it instead of consuming
  a false empty truth.
- **Empty test face / zero execution evidence**: each `gut_full` arm discloses
  `collected_scripts` and `execution_stream`. Zero collected scripts →
  `invalid_reason=empty_test_face`; scripts were collected but no GUT test
  stream was observed → `invalid_reason=no_execution_evidence`. Both are
  invalid arms and cannot take the acceptance-paused single-arm exit-`0` path.
- **Arm `invalid_reason` classification** (execution/failure classes use
  priority order `launch_failure > shard_timeout > unattributed_failures >
  new_failures > reconciliation_failed`; an invalid arm carries the first
  matching class and the corresponding `invalid_evidence` block):
  - `launch_failure` — the Godot subprocess never reached GUT (looked like a
    binary / project-load failure). Evidence: `launch_failures=[shard ids]`.
  - `shard_timeout` — a shard exceeded its wall-clock budget. Evidence:
    `shard_timeouts=[shard ids]`.
  - `unattributed_failures` — failing tests whose `(script, test)` identity
    could not be attributed to a recorded baseline pair. Evidence:
    `unattributed=[(script, test), ...]` (top-N of total).
  - `new_failures` — failing tests not present in the baseline and not routed
    to suspected instrumentation intolerance. Evidence:
    `new_failures=[(script, test), ...]`.
  - `reconciliation_failed` — the run produced verdicts but the reconciliation
    pass disagreed (e.g. reported ok vs. observed line coverage diverged).
    Evidence: `reconciliation_failed=true`.
  The evidence block is disclosure-only and never overrides the priority
  ordering. An invalid arm never contributes to the union and is not scored.
- **Failure identity normalization**: an attributed `(script, test)` pair and
  its bare-name echo count once in baseline counts and disclosures; the
  attributed form is retained. Same-named tests in different scripts remain
  distinct identities.
- **Lazy baseline absorption disclosure**: when lazy acquisition absorbs one or
  more pre-existing red pairs, `baseline_failures_meta` carries
  `status=partial` plus the named `excluded_tests`, and stderr discloses
  `EXCLUDED by name`, matching the preflight path. Zero absorbed pairs do not
  add a partial marker.
- **Suspected instrumentation intolerance**: on the attribution auto-build
  path, an instrumented failure that passed on the uninstrumented baseline
  interrupts with `suspected_instrumentation_intolerance` and a ready-to-save
  `--intolerant-tests` draft. On the `measure run` lazy-baseline path, those
  proven pairs move out of `new_failures` into
  `instrumentation_intolerant.observed`, with `suspected_draft` in the report
  and the same classification/draft on stderr, so they do not invalidate the
  arm. Lazy acquisition failure has no such proof and stays fail-loud with an
  invalid arm; acceptance-arm failures remain visible and never enter this
  lane. Save the draft and pass it via `--intolerant-tests <path>` (or
  `GQT_MEASUREMENT_INTOLERANT_TESTS=<path>` for auto-build) to declare the
  tests as coverage blind spots.
- **Intolerant declaration schema**: `--intolerant-tests` uses
  `gherkin_scenarios[]`; each entry requires non-empty `feature`, `scenario`,
  and `reason`, with optional `scenario_line` and `example_row_line`. The
  retired `acceptance_scenarios` key fails loudly instead of being ignored.

## Boundaries

- `measure run` temporarily instruments the target tree and restores it
  atomically in `finally`; no residue on production trees.
- A restored Gherkin arm uses the same addon preflight as `gherkin run`:
  `compile_preflight` and per-key replay run before one `validator.gd` launch,
  which in turn precedes shared GUT fan-out. A failed validator marks the arm
  invalid and leaves no tool-owned private-suite residue. This addon handshake
  is distinct from the uninstrumented `baseline_preflight` described above.
- The acceptance arm runs **shared shards** (SEE-1619): features are grouped
  into private suites so several keys share one GUT process, with real fan-out
  `len(groups) × reps` instead of one process per feature per rep. A feature
  only falls back to a singleton process when it fails shared validation this
  run; such downgrades are disclosed by name in `process_fallbacks:[{key,reason}]`
  (sorted by key, present on every return path, empty when fully atomic), never
  packaged as valid when both shared and isolated runs are red, and their
  concurrency is still capped by `--gherkin-shards`. Only tool-owned private
  suites are deleted; caller-owned directories are preserved. Cleanup failure
  marks the arm invalid and discloses the residue path.
- The W4 probe (default `aidomain_integration`) records measured wall clock
  into `performance.gherkin_probe`; `measure risk --performance-file`
  accepts externally fed measurements.
- One map per project, at `.qa-cache/attribution-map.json`. Its location is
  decided by code: no flag or environment variable points the tool at another
  file, and the retired spellings `--attribution-map`, `--no-attribution-auto`,
  `--test-attribution-out`, the `map-update` positional roots, and its
  `--recipe` / `--baseline` / `--changed` / `--out` / `--calibration-every` /
  `--attribution-causal` arguments are argparse usage errors (exit `2`, no
  stdout JSON). `mutation`, `measure coverage`, `measure risk`, and `measure
  map-update` renew the map during preparation and only read it while running.
- The map's status is a read-only probe inside the tool (zero renewal, zero
  GUT, zero import, zero writes), not a CLI command. It returns `present`,
  `fresh`, `factors`, `would_refresh`, `scope`, and `last_cost`, and
  `would_refresh` always equals what the refresh then does (`create` /
  `increment` / `full`). Every stale factor is named by prefix — `schema:`,
  `arms:`, `recipe:`, `tests_face:`, `content:`, `test_verdicts:`,
  `feature_sha256:`, `feature_sha256_missing:`, `report_recipe_hash:`,
  `view:`, `increment_not_eligible:`. Users meet them through
  `mutation --dry-run`: the `attribution_map_missing_or_stale` plan problem
  carries them as `evidence.reason`, with `evidence.would_refresh` and
  `evidence.last_cost`.
- `GQT_ATTRIBUTION_MAP_MODE=off` is the diagnostic switch that suppresses
  automatic map renewal: an existing fresh map is still consumed, but a
  missing map is a named error (`no_attribution_map`, or
  `prepare renewal produced no map at the default path` from
  `measure map-update`) instead of being built.
- Scope rules live in `<root>/.gqt-scope.json`. `production.include` is a
  whitelist of `{kind: dir|file|ext, value}` entries; `production.exclude` is
  a blacklist whose every entry carries a `reason`; the test whitelist is read
  from `.gutconfig.json` and the gherkin features directory, never configured
  twice. `tests.exclude` (`dir` or `feature` kinds, each with a `reason`)
  really removes those tests from the face — `measure scope` reports them under
  `gut_dirs_excluded` / `features_excluded` and lists the files as `non_test`.
  An absent rules file is legal: the generic default applies (everything
  non-hidden is production; gutconfig dirs and features are tests) and
  `measure scope` says so in `unconfigured_hint`. A misspelled path, an empty
  whitelist, an exclude without a reason, or an unreadable file is a named
  failure (exit `1`). The rules file is bound into the recipe, so changing it
  makes the map stale (`recipe:` plus `report_recipe_hash:` factors, full
  rebuild). The map universe is the full set these rules resolve.
- The `/7` attribution map stores `test_to_lines`, `script_to_lines`,
  `line_to_tests`, and the instrumentable line surface. File/function views are
  derived from those lines plus the source registry, not independently measured.
- Mutation projects the scripts in its actual `tests_glob` into a
  `see1420-coverage/2` entry from the same `/7` facts. A persisted projection —
  bound to the current map's content and execution evidence (SEE-1594 A05) —
  or a fresh map can serve the prefilter without another instrumentation pass;
  binding drift invalidates the projection and is disclosed.
- The manual `see1420-coverage/1` store is retired (SEE-1594 A04): mutation
  prefilter no longer reads it, and no writer produces it. Leftover `/1`
  files on disk are rejected by schema. Coverage answers come from the
  line-level map (single source of truth).
- Schema upgrades are fail-loud: an old-schema or otherwise unusable map is
  judged not fresh and rebuilt by the next preparation.
- The internal `DEFAULT_RERUN_FULL_FACE_RATIO` is `0.05`: if the candidate
  rerun scripts exceed 5% of the baseline script face, the refresh falls back
  to a full rebuild, reported as `reason: rerun_surface_too_large_fallback`.
  This was calibrated on a warm KOL copy (824 files / 285
  scripts): incremental cost was approximately `16.16s + 8.878s × rerun
  scripts`, full-face cost was `223–227s`, and the crossover was about 23.6
  scripts (8.3%). The 5% cap estimates about 140.5s at the boundary, below the
  225.25s full-face midpoint and with noise-band headroom.
- The Gherkin leg has its own `0.05` cap
  (`gherkin_rerun_surface_too_large_fallback`), and a full calibration rebuild
  is due once `increments_since_full` reaches 5 (`calibration_due_fallback`).
- Each arm carries `unattributed_files`: production files with unattributed
  hits, for which test-window attribution is lossy. Incremental artifacts carry
  the union of the baseline list and this round's changed/new production files.
  This is measurement-completeness disclosure only: mutation's strict per-test
  filter does not consume the list or widen selection to function/file faces.

## Acceptance performance harness (SEE-1619)

`tests/integration/run_see1619_gherkin_perf.py` compares the **shared-shard
and per-feature process shapes within the same gqt branch** (not a cross-version
master comparison) on the same KOL input, reps, concurrency and an isolated
store, never extrapolating wall clock from process counts:

```bash
GQT_MEASUREMENT_GHERKIN_DISABLED=0 PYTHONPATH=src python3 -I tests/integration/run_see1619_gherkin_perf.py \
  --project-root <kol-copy> --godot "$GODOT" --aa 3 --rounds 3 \
  --report <work>/acceptance-perf.json
```

The environment prefix restores the Gherkin arm, which is otherwise paused by
default. Reports use `doc="see1619-gherkin-perf/1"` and per-round
`gherkin_wall_s`; resume rejects the retired `see1619-acceptance-perf/1`
document instead of mixing old and new rounds.

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

## Command reference (aligned with the current `--help` output)

### measure suite

`gqt measure suite [--project-root <root>] [--out recipe.json] [--exclude scope:entry:reason ...]`

| Argument | Default | Meaning |
|---|---|---|
| `--project-root <root>` | discover from CWD | Target Godot project root; explicit value wins and invalid values fail loud |
| `--out <path>` | `./recipe.json` | Recipe JSON output path |
| `--exclude <scope:entry:reason>` | repeatable | Exclude a GUT dir or acceptance feature; an empty reason fails loud |

### measure scope

`gqt measure scope [--project-root <root>]`

| Argument | Default | Meaning |
|---|---|---|
| `--project-root <root>` | discover from CWD | Target Godot project root; an explicit value that does not exist is a named failure (exit `1`) |

Read-only and zero-write: prints the four class lists and counts produced by
`.gqt-scope.json`, the per-directory counts, the excluded entries with their
reasons, and any reference warnings. Output keys are listed under
[Output & exit codes](#output--exit-codes).

### measure run

`gqt measure run <roots...> [--project-root <root>] --recipe <recipe.json> --out <report.json> [all options]`

Acceptance-specific repeat/shard/adaptive/parallel knobs apply only when the
acceptance arm is explicitly restored; while paused, that arm stays disabled.
The retired `--acceptance-probe-keys`, `--acceptance-shards`,
`--acceptance-repeats`, and `--acceptance-adaptive` spellings are not aliases:
they are argparse usage errors (exit `2`, no stdout JSON).

| Argument | Default | Meaning |
|---|---|---|
| `<roots...>` (positional) | required | Target `.gd` roots (recursive glob); `gut_full` runs the full gutconfig face, while acceptance runs only when explicitly restored from its default pause |
| `--project-root <root>` | discover from roots/CWD | Project root containing `addons/gut` + `.gutconfig.json`; explicit value wins |
| `--recipe <path>` | required | `measurement-suite/3` JSON from `gqt measure suite` |
| `--out <path>` | required | `measurement-report/3` output JSON path |
| `--baseline-failures <path>` | omitted | JSON of pre-existing un-instrumented failing `(script, test)` pairs; new failing pairs gate the arm; bare names are rejected as ambiguous |
|  |  | Without it, the gut_full arm **lazily** acquires the baseline only when the instrumented arm observed failures (one same-face uninstrumented GUT run; green arms pay zero extra GUT); acquisition failure is fail-loud and the arm stays invalid. With `--test-attribution`, the emitted attribution map records a `baseline_failures_meta` disclosure key (`method` / `face` / `count` / `sha256` / `wall_s`) describing how that baseline was acquired; the key is carried across `measure map-update` increments and is disclosure-only (consumers do not read it). The acceptance arm is never baselined — its failures stay visible |
| `--intolerant-tests <path>` | omitted | JSON declaring instrumentation-intolerant tests and coverage limitations; declared tests bypass failure gating by name and are disclosed as coverage blind spots |
| `--gherkin-probe-keys [keys]` | `aidomain_integration` | W4 gherkin cost-probe keys |
| `--timeout <s>` | 180 | GUT timeout seconds per arm (must be ≥1); zero or negative values are usage errors: exit `2`, readable stderr, no stdout JSON |
| `--stability-baseline <path>` | omitted | Prior measurement report; fids whose covered lines differ route to the unstable lane (no score) |
| `--shard-width <n>` | 6 | gut_full parallel shard count (1 = sequential) |
| `--gherkin-repeats <n>` | 3 | Acceptance arm runs each shard group N times and unions the line sets; convergence round disclosed |
| `--gherkin-shards <n>` | 4 | Acceptance parallel shard count |
| `--shard-log-dir <dir>` | omitted | Retain per-shard full logs and raw sink copies |
| `--evidence-dir <dir>` | omitted | Per-arm `coverage-evidence/2` bundle + run record output dir |
| `--test-attribution` | off | Emit a line-primary `test-attribution/7` artifact with per-test/per-script covered lines, reverse lookup, derived file/function views, a `test_verdicts` table, `identity_collisions` disclosure and execution evidence. When off, no attribution artifact is written; while acceptance is paused the ordinary run uses the documented single-arm exit semantics. The artifact path is `<--out>.attribution.json`, decided by the tool and disclosed as `summary.attribution_map` |
| `--attribution-causal` | off | Also wrap `call_deferred/create_timer` creation sites so their callbacks are attributed to the creating test; requires `--test-attribution` |
| `--gherkin-adaptive` | off | Run acceptance reps round-by-round and stop early once the line-set union stabilises (bound stays `--gherkin-repeats`) |
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
measure: arm=gherkin start shards=4 reps=3
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
| `--features <k1,k2>` | required | Comma-separated acceptance feature keys; recipe keys and requested keys (including `a+b` members) normalize non-`[A-Za-z0-9_]` characters to `_`, so `my-demo` and `my_demo` resolve identically |
| `--out <path>` | required | Diagnostic report JSON path (progress written after each feature) |
| `--timeout <s>` | 480 | Per-leg GUT timeout seconds |
| `--godot-bin <path>` | omitted | Per-command Godot binary override |

### measure map-update

`gqt measure map-update [--project-root <root>] [--timeout 600] [--godot-bin <path>]`

| Argument | Default | Meaning |
|---|---|---|
| `--project-root <root>` | discover from CWD | Target Godot project root; explicit value wins |
| `--timeout <s>` | 600 | Refresh timeout in seconds |
| `--godot-bin <path>` | omitted | Godot binary for the toolchain probe |

A thin adapter over the map contract's refresh: one call creates, increments,
or fully rebuilds the project's unique map, or reports it already fresh. No
map paths are taken and no change list is supplied — the refresh derives the
invalidation surface from the map's own bindings (recipe and toolchain hash,
test face, production and feature content). The `summary` keys and the
increment ledger are described under [Output & exit codes](#output--exit-codes).
