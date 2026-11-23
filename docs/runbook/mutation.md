# mutation — mutation testing

## When to use

Verify that your test suite actually catches source defects: generate
source-level mutants for a `.gd` file, execute each in an isolated shadow slot,
and report every mutant the tests fail to kill as a survivor (an assertion
gap).

## Plan-first flow

`gqt mutation` runs in three stages on every invocation (including the
default non-dry-run path):

1. **plan** — a static, read-only sweep bounded by `PLAN_BUDGET_S=10s`. It
   inspects `project.godot` autoload targets, every `preload`/`load` call in
   the test surface, the Godot binary and the GUT addon, and the state of the
   attribution map. It enumerates missing `.gutconfig.json`,
   `acceptance/features/`, and `.feature` prerequisites in one pass, and
   validates an explicit `--tests` glob before any GUT leg. A missing
   single-file target fails before map build/prepare. Each finding is recorded
   as a problem entry `{severity, code, evidence, fix}` in `plan.problems[]`.
   Errors block the run; warnings disclose work the next stage will pay for.
   A read-only `estimated_costs` list previews expensive operations without
   executing them.
2. **prepare** — performs the work plan disclosed: import cache warm-up,
   attribution-map build / incremental refresh, baseline preflight, warm pool
   provisioning. Anything that fails here is named (`route_drift`,
   `baseline_preflight_red`, `suspected_instrumentation_intolerance`,
   `plan_blocked` if a plan error surfaced late, …) instead of collapsing
   into a generic `run_error`.
3. **run** — consumes the prepared assets. The static plan output is reused
   for both `--dry-run` (plan only, never enters prepare) and the real run.

`--dry-run` therefore does **not** require an attribution map. When no usable
map exists, the response is `ok=true`, `selection_mode=plan_only`, and the
plan's `problems[]` contains an `attribution_map_missing_or_stale` warning
describing the work a real run will pay for; nothing is renewed, imported, or
censused. Pass `--select full` or an explicit `--tests` to preview sites
without a map. Dry-run is read-only and is not poisoned by an earlier run's
route state; a spurious `route_drift` does not block it, while a genuine
authoritative route mismatch still fails loud. If the plan finds an
error-class problem the run aborts as `plan_blocked` with
`summary.error="plan_blocked"`, `summary.plan` echoing the static plan, and
one `failures[]` entry per error problem.

When the target file paths are not all under the project root (or are bare
top-level files), target derivation falls back to the project root and
discloses this on stderr as `measure: targets_root_fallback (<reason>) —
target face widened to the whole project root ...`. The fallback is named,
not silent.

## Prerequisites

- `<root>/addons/gut/gut_cmdln.gd` exists; a resolvable Godot binary
  (`--godot-bin` > `GQT_GODOT_BIN` > PATH).
- With `--tests` omitted, selection uses the line-primary runtime truth map
  (`test-attribution/6`): per-test/per-script covered line sets are the primary
  facts; file/function views are derived from them. By default the map is
  auto-managed at `.qa-cache/attribution-map.json` and regenerated or
  incrementally refreshed when missing or stale. You can instead pass an
  explicit read-only artifact with `--attribution-map`, or build one with
  `gqt measure map-update`; an explicit schema mismatch fails loud without an
  automatic rebuild.

## Typical examples

```bash
# Default: line-level truth-map selection + differential resume +
# coverage prefilter (all automatic)
gqt mutation src/save/save_manager.gd --project-root . --budget 50

# Selection: line (default) / fn and file (explicit calibration/fallback) /
# full (explicit full-face audit, bypasses the mapping)
gqt mutation src/save/save_manager.gd --project-root . --select fn

# Explicit test subset (user takes over; disclosed as explicit_tests)
gqt mutation src/save/save_manager.gd --project-root . --tests res://tests/save/

# Count sites only; caliper reruns bypass diff reuse and the prefilter.
# --lines accepts one line, a range, or comma-separated segments.
gqt mutation src/save/save_manager.gd --project-root . --dry-run
gqt mutation src/save/save_manager.gd --project-root . \
  --mutation "src/save/save_manager.gd#func:save#2"
gqt mutation src/save/save_manager.gd --project-root . --lines 10-20,30,35-40

# Parallelism (default jobs = max(2, cpu//2)); force serial with --jobs 1
gqt mutation <file> --project-root . --jobs 8

# Opt-outs for troubleshooting
gqt mutation <file> --project-root . --no-attribution-auto
gqt mutation <file> --project-root . --no-reuse-coverage
GQT_MUTATION_WORKER=0 gqt mutation <file> --project-root .
```

## Selection semantics

- `line` (default): each mutant is associated with the tests that executed its
  source line. Sites whose `frozenset(line→tests)` signatures match share one
  materialized test glob and one baseline; grouping is by distinct test set, not
  mechanically by line. Sites with no mapped tests are disclosed as `uncovered`
  and do not execute on this default path.
- Test-level filtering is ON by default for controlled line faces: `tests_allow`
  contains only exact `line_to_tests` identities plus whole-script entries for
  `script_to_lines` lanes. Sparse lines do **not** widen to function/file test
  sets, and `unattributed_files` never changes selection. When an allowlist is
  applied, `summary.test_filter.scope` is always `line`; disable the filter for
  troubleshooting with `GQT_MUTATION_TEST_FILTER=0`.
- `fn` / `file`: explicit calibration and fallback views, not parallel primary
  semantics. `fn` groups by enclosing function and discloses a bounded file
  fallback when the function is absent; `file` uses the file's derived test set.
- Calipers: `--lines` intersects requested lines with actual sites and reports
  `requested / hit / skipped_no_site`; an entirely site-less request fails loud.
  `--lines` and `--mutation` use the union of the selected lines' test sets.
  A selected site with no mapped tests still executes and discloses
  `no_mapped_tests`, preserving the fair rerun channel.
- Freshness: the map must match the current recipe, target-file sha256 and test
  face, and contain execution-integrity evidence. Zero tests or no probe
  evidence is not a valid empty map. In auto mode a missing, stale, or old-schema
  map is regenerated or incrementally refreshed once, then re-judged; an
  explicit `--attribution-map` remains read-only and fails loud on mismatch.
  Auto builds gate on new test failures only (§SPEC-060a): pre-existing
  failures are absorbed by a lazily acquired uninstrumented baseline (one
  extra same-face GUT run, triggered only when the instrumented arm observed
  failures; disclosed as `baseline_failures_meta` on the map), while any
  new failure keeps the build fail-loud.
- Explicit `--tests`: the caller takes over test selection; the report uses
  `test_selection.source=explicit_tests` to disclose that line-level selection
  was not used. This face does not require an attribution map and bypasses the
  map-ensure/P1 prepare gate. A missing explicit glob is a plan error
  (`tests_glob_target_missing`) before any GUT leg.
- An explicit `--attribution-map` stays read-only. Its preparation guidance
  names repairing or replacing the map; it never suggests `--auto-prepare`,
  which would contradict that read-only contract.
- `summary.selection_mode` is the **effective** face, one of `line` / `fn` /
  `file` / `full` / `lines` / `mutation` / `explicit_tests` (caliper beats
  explicit `--tests`, which beats `--select`). When line/fn slicing is capped
  by the signature-group count, the report sets `jobs_limit_reason=
  selection_groups` and `jobs=selection_groups`.
- Zero references: a fresh mapping that says no test touches the target is a
  terminal short-circuit (`summary.zero_ref_short_circuit=true`, zero
  execution, `ok=true` disclosure) — not a full-suite fallback. The no-sites
  path (zero tests and zero mutation sites) carries the same key plus
  `summary.short_circuit_reason=no_mutation_sites`.
- `full`: the only explicit audit face (`tests_glob=None`); use only when you
  deliberately want the whole suite. GUT collection includes test
  subdirectories recursively (`-ginclude_subdirs`). A worker leg that actually
  ran zero tests is a named `run_error`, not a survivor; the L4 evidence gate
  remains fail-closed.
- `--jobs 1`: the explicit serial path still uses a file-level union glob. It is
  a correct superset, but less converged than line-signature grouping.

## Output & exit codes

- `tool` is always `mutation`.
  `ok = survived==0 ∧ timeout==0 ∧ run_errors==0 ∧ suspect==0`;
  `invalid_mutant`, `timeout_pinned` and `uncovered` never enter the ok gate.
- Core `summary` keys: `{file, mutants, killed, survived, timeout, run_errors,
  kill_rate, budget, jobs, suspect, invalid_mutants, adaptive_timeout_s}`.
- Diff / prefilter / selection keys: `retry_total`, `executed`,
  `untouched_ranges`, `diff_skipped`, `total_sites`, `attention`,
  `coverage_prefilter`, `lines_selection`, `selection_mode`, `selection_groups`,
  `jobs_limit_reason`, `test_selection`, `attribution`,
  `zero_ref_short_circuit`, `short_circuit_reason`.
- Kill-bail keys (SEE-1606 P3): `kill_bail`, `kill_bail_face`, `kill_bail_disabled_by`, `kill_bail_scripts` — always present on the normal path; killed details carry `kill_bail_info` (scripts_run / scripts_total / new_failure). No new `exec_scheme` token: early bail is internal behaviour of the existing schemes, cache identity unchanged.
- Baseline-skip keys (SEE-1606 P2-R1): `baseline_skip {applied, face, requested}`
  on the normal path when the baseline is dirty; `face` is the effective
  execution face. A worker-to-cold downgrade also discloses `planned_face`,
  `degraded`, and `degrade_reason`; `applied=true` only when that effective
  face actually skipped red tests. Mutant details carry
  `skip_red {marked, requested, names}`. Suspect is suppressed on skip-enabled
  batches (empty diff = genuine survive).
- Baseline-failure keys (SEE-1606 P2): `baseline_dirty`, `baseline_failures`,
  `baseline_failure_names`, `break_on_baseline_failure` — always present on the
  normal path; a dirty baseline adds its failing tests to this registry and
  they never count toward a kill.
- Mechanism keys: `worker_channel` (`requested` / `route` / `exec_scheme` /
  `decision` / `reason` / `route_reasons` / `reason_counts` / `closure_size` /
  `override_refused`; degraded adds `fatal_events` / `equivalence`), `farm`
  (`mode` / `decision` / `reason` / `proof_binding` / `proof_reason` /
  `validation` / `generated_dirs`), `prewarm` (`decision` / `reason` /
  `import_calls` / `probe_s` / `import_wall_s`), `stage_reconciliation`
  (`wall_s` / `attributed_s` / `unexplained_s` / `over_attributed_s`).
- `execution_plan` is a read-only five-group view (`selection`, `route`,
  `budget`, `diff`, `preconditions`) on normal and dry-run reports. Dry-run also
  emits `execution_plan_meta`: derived/observed/runtime partitions, a `plan_id`
  hashed only from derived inputs, channel/reason/failure/reuse answers, and a
  read-only `map_state`. Missing or stale maps are disclosed there as
  `would_renew` plus `missing` and `cost`; no renewal/import/census is run, and
  this disclosure does not replace selection fail-loud behavior. When the
  dry-run returns the static plan only (no usable map), the top level carries
  `summary.plan` (the static plan, including `problems[]`) and
  `summary.selection_mode=plan_only`, and stops short of site enumeration.
- `equivalent_pruned` appears only when `GQT_MUTATION_EQUIV_PRUNE` opts in. It
  discloses rule counts, before/after denominators and every pruned stable id;
  kept ids and their order are unchanged. KOL samples with no provable matches
  report zero pruned items rather than a fabricated speedup.
- Single-file runs always use the flat `{tool, ok, summary, failures}`
  envelope, including `--jobs 1`; `{tool, ok, jobs, results[]}` exists only for
  multi-file runs. Planning outcomes (`plan_only` / `prepare_required` /
  `plan_blocked`) keep `plan` inside `summary` and always include `failures[]`,
  including multi-file planning failures (which may also retain `results[]`);
  the old top-level `plan` / `error` keys are not emitted.
  `coverage_prefilter.source` distinguishes `projection_store` (persisted
  `see1420-coverage/2` derived cache bound to the current map's content and
  execution evidence) and `projection_live` (fresh line facts projected on
  demand). SEE-1594 S2 retired the manual `see1420-coverage/1` store layer;
  leftover `/1` files on disk are rejected by schema and never consulted.
- `failures` lists only non-killed records (each with an `id`), verdicts in
  survived / timeout / run_error / suspect / invalid_mutant / timeout_pinned /
  uncovered. Verdict and kind are open enums.
- exit `0` = pass; `1` = verdict fail / abort; `2` = argument error.

## Failure semantics

- **Abort shape**: baseline timeout or untrustworthy →
  `summary.{file, run_error: true, error}` with no killed keys; check
  `summary.run_error` before reading metrics. A baseline timeout also carries
  `suggested_action` + `suggested_action_kind`: `raise_timeout` (below the
  600s cap) or `narrow_face` (at/over the cap — `--select full` runs the whole
  GUT suite, so switch to `--select line|fn` or a `--lines`/`--mutation`
  caliper face instead of re-running with the same timeout).
- **Kill = newly-failing test-name set relative to baseline**, never the GUT
  exit code — a baseline with pre-existing failures would otherwise fake
  kills.
- `suspect` = dirty baseline with an empty difference set; `timeout_pinned` =
  timeout in 2+ consecutive runs (always re-run, visible, outside the gate);
  `uncovered` = the coverage prefilter judged the line uncovered (a quality
  signal: not executed, no budget consumed).
- Selection fail-loud (`MutationSelectionError`) → exit 1, stating why the
  mapping is unusable.
- The first parent identity/file-key route is authoritative. Core setup,
  pre-baseline, slot acquisition and worker injection only verify it. Any
  mismatch aborts before baseline/cache/engine work with
  `summary.{run_error:true,route_drift:true,authoritative_scheme,resolved_scheme,drift_site}`;
  never continue on the drifted channel.
- **Suspected instrumentation intolerance**: when an instrumented run
  produces failing `(script, test)` pairs that pass on the uninstrumented
  baseline, the run interrupts with
  `classification=suspected_instrumentation_intolerance`, the named failing
  pairs, the wall clock and face of the uninstrumented baseline, and a
  ready-to-save `--intolerant-tests` declaration draft
  (`{"tests":[{"name":...,"reason":...,"script":...}], "coverage_limitations":[]}`).
  Options printed alongside the draft: (a) save the draft and re-run with
  `--intolerant-tests <path>` to declare those tests as blind spots, or
  (b) investigate the instrumentation itself. Save the draft to a path and
  point `GQT_MEASUREMENT_INTOLERANT_TESTS=<path>` at it so the
  measurement-chain auto-build reuses the same declarations.

## Boundaries

- The real repo is never written: mutants land only in disk shadow slots.
  Linux and win64 share one persistent warm pool
  (`<root>/.qa-cache/mutation-pool/slots/`); a one-shot fresh ext4 farm is used
  only when the pool lock is unavailable, and the fallback is disclosed as
  `farm.reason_detail=pool_lock_unavailable`. Every reuse/rebuild carries a
  machine-readable `farm` reason (`warm_reused` / `freshness_ambiguity` /
  `generated_dirs_missing` / `proof_drift_rebuild` / …). A four-level beacon
  proves the engine actually executed the mutant.
- Coverage prefilter resolves in order: persisted `/2` projection → live
  projection from the fresh `/6` map. The projection uses the scripts carried
  by the actual `tests_glob`; it adds no second instrumentation or GUT run.
  The retired manual `/1` layer is never read; source, test-face, recipe,
  schema, binary or identity drift invalidates the affected layer and is
  disclosed. If all layers are unusable, execution continues unfiltered rather
  than producing false coverage.
- `elif` condition lines participate in those coverage facts, so they are not
  falsely classified `uncovered` and skipped. Coverage means the condition was
  true and its branch body executed; a false-path evaluation alone is not
  counted. `else` header lines remain outside the instrumentable set.
- Differential resume is on by default: unchanged scopes with
  killed/suspect/invalid verdicts are reused; survived/timeout/run_error sites
  always re-run (red line); `--mutate-all` forces full execution. The
  content-addressed line-group glob participates in the resume key, so upgraded
  or changed groups naturally miss old entries and rerun once; no stale verdict
  is reused across different test sets.
- `# gqt: no-mutate` filters sites (on or directly above a top-level `func` =
  function-level, anywhere else = line-level).
- Caliper reruns (`--mutation` / `--lines`) bypass diff reuse and the prefilter
  — the fair spot-check channel against false negatives. They still use the
  selected lines' runtime test sets unless `--tests` explicitly takes over.

## Opt-in acceleration and host waits

- History-killer ordering is **OFF by default**. Set
  `GQT_MUTATION_TEST_ORDER=history` to order scripts by historical kill counts;
  ties and missing history retain deterministic original order. The kill-bail
  chunk remains **C=32** by default (`GQT_MUTATION_BAIL_CHUNK` overrides).
  Interleaved measurements found no stable over-noise ordering gain; enabling it
  may change the first killer, not the stable-id verdict.
- Equivalent-mutant pruning is also **OFF by default**. Set
  `GQT_MUTATION_EQUIV_PRUNE=1` to prune only the three constructively proven
  forms: integer-literal `n+0` (`+→−`), `n−0` (`−→+`), and identical ternary
  arms. Dynamic/non-literal/asymmetric forms are retained. Pruning happens
  after stable-id assignment, so retained ids do not shift.
- Direct-drive and worker `end_run` waits use signals plus a one-shot timeout,
  with deferred coroutine resumption; direct-drive defaults to 120s and the
  worker fallback wait to 60s. Main-loop guards use one 100ms timeout. The only
  remaining host poll is the worker's 10ms req/STOP filesystem rendezvous,
  which has no one-shot filesystem event API.

## Command reference (aligned with v0.4.0 `--help`)

`gqt mutation [<files...>] [--project-root <root>] [all options]`

| Argument | Default | Meaning |
|---|---|---|
| `<files...>` (positional) | git diff HEAD when omitted | `.gd` files, or one compact `<file>.gd#<lines>` address; omitted = tracked changes plus untracked `.gd` files, excluding hidden/tool/vendor paths |
| `--project-root <root>` | discover from target/CWD | Project root containing `addons/gut/`; explicit value wins and invalid values fail loud |
| `--budget <n>` | 50 | Max mutants (must be ≥1); with diff reuse present, counted on the post-diff retry set (order-preserving truncation). Zero or negative values are usage errors: exit `2`, readable stderr, no stdout JSON |
| `--timeout <s>` | 600 | Single GUT wall-clock limit (must be ≥1); default equals the budget cap so autoload-scale file-union faces (~77s measured on KOL) no longer trip the baseline-snapshot timeout and its documented serial downgrade. Zero or negative values are usage errors: exit `2`, no stdout JSON |
| `--dry-run` | off | Run the static plan only; zero GUT, map renewal, import or census unless `--check-baseline` explicitly requests the one read-only baseline leg. The plan response carries `problems[]` (severity/code/evidence/fix per finding) and `estimated_costs`, plus the diff-estimate, `execution_plan` / `execution_plan_meta` views. A missing or stale attribution map does not block the dry-run: the response is `ok=true` with `selection_mode=plan_only` and the plan's `problems[]` records `attribution_map_missing_or_stale` as a warning. Error-class problems abort as `plan_blocked`. Existing selection fail-loud semantics still apply when a usable map is present and `--dry-run` is asked to enumerate sites. Caliper addressing is applied before estimation, so `mutants` / `executed_estimate` equal the selected site count; an entirely site-less `--lines` request or stale `--mutation` id fails with the same named error as a real run |
| `--check-baseline` | off (`not_checked`) | In dry-run/plan-only form, run one uninstrumented baseline GUT and disclose `summary.static_preconditions.baseline_health`; without this flag no baseline leg runs |
| `--auto-prepare` | off | Automatically prepare P1 blockers disclosed by the plan; explicit read-only attribution-map guidance never recommends this flag |
| `--intolerant-tests <path>` | omitted | JSON declaring instrumentation-intolerant tests and coverage limitations; auto-build interruption reports provide a ready-to-save draft |
| `--fail-on-red-baseline` | off | Fail fast on a red uninstrumented baseline; shares the same abort channel as `--break-on-baseline-failure` |
| `--tests <res://glob>` | omitted = line-primary truth-map selection | Explicit test subset; takes over from the mapping, discloses `explicit_tests`, and does not require map ensure/P1 preparation. A missing glob is rejected during plan as `tests_glob_target_missing`, before any GUT leg |
| `--jobs <n>` | `max(2, cpu//2)` | Worker pool size (must be ≥1) for per-file AND per-mutant slicing; `--jobs 1` forces the serial file-union boundary; sites ≤ jobs falls back to serial automatically. Single-file output stays flat for every `--jobs` value. Zero or negative values are usage errors: exit `2`, no stdout JSON |
| `--resume` | **retired** (SEE-1594 A13) | Removed: differential reuse is automatic whenever a v2 manifest exists |
| `--select {line,fn,file,full}` | `line` | line = test-set signature groups; fn/file = explicit calibration/fallback views; full = explicit full-face audit. The reported `selection_mode` also uses `lines` / `mutation` for calipers and `explicit_tests` when `--tests` takes over |
| `--attribution-map <path>` | auto-managed `.qa-cache/attribution-map.json` | Explicit `test-attribution/6` line-primary artifact; read-only pass-through, schema mismatch fails loud without rebuild |
| `--no-attribution-auto` | off | Disable automatic map generation/incremental refresh; with no usable map the run fails loud |
| `--mutation-warning <n>` | 50 | Warn on stderr + `summary.attention` when the pre-truncation site count exceeds N |
| `--timeout-factor <f>` | 2.0 | Adaptive per-mutant timeout = `max(timeout, baseline×f+1)`; only widened, never tightened |
| `--no-kill-bail` | off (bail ON) | SEE-1606 P3, aligns with Stryker `disableBail`: with bail on, a mutant's selection face runs the selection face in script order inside ONE engine process in chunks of 32 scripts (cold one-shot direct-drive host for materialized selection dirs / single-script faces; in-session sequencing for the resident worker) and stops after the first chunk containing a failure NEW vs baseline — killing legs save the remaining chunks (≤31-script overshoot bound; chunking chosen by measurement: per-script calls made a 286-script survivor leg 2.69× slower, C=32 gives 0.81×) (`GQT_KILL_BAIL scripts=k/n new_fail=<test>`); verdicts unchanged (difference-set semantics). Unsupported faces (explicit `--tests` dir / `--select full`), missing real `gut.gd`, flag or env `GQT_MUTATION_KILL_BAIL=0` disable bail with named `kill_bail_face` / `kill_bail_disabled_by` disclosure — never a silent fallback |
| `--break-on-baseline-failure` | off | SEE-1606 P2 + P2-R1: when off (default) baseline-red tests are **physically skipped** from every mutant execution face at test granularity (removed from the GUT collector inside controlled hosts, matched by script identity + test name (same-named green tests in other scripts keep running and keep killing — no false survivors); marker `GQT_SKIP_RED marked=k requested=n names=…`, `summary.baseline_skip`, per-verdict `skip_red`; red names absent from the run stream = machine evidence of not-called). With skip active an empty diff is a genuine survive (suspect suppressed). Cold-legacy faces (explicit `--tests` dir / `--select full` / missing real `gut.gd`) cannot per-test filter: a dirty baseline aborts fail-loud (`baseline_skip_unsupported` + suggested action). When on, a dirty baseline aborts before any mutant GUT |
| `--mutate-all` | off | Force full execution (the only escape from diff reuse) |
| `--mutation <file>#<scope_id>#<n>` | omitted | Single-site caliper (single file): fresh execution using that site's line-level tests; bypasses diff reuse and prefilter |
| `--lines <spec>` | omitted | Single line `42`, range `10-20`, or multi-segment `10-20,30,35-40`; intersects actual sites, discloses no-site lines, and uses the selected lines' test union. A malformed spec is a usage error (exit `2`, no stdout JSON); a valid spec with zero sites fails with exit `1`, including dry-run |
| `--reuse-coverage` | **retired** (SEE-1594 A13) | Removed: prefilter is on by default; use `--no-reuse-coverage` to disable |
| `--no-reuse-coverage` | off | Explicitly disable the coverage prefilter, including store and live-projection layers |
| `--worker-channel` | on by routing | Force the resident worker; static routing sends autoload/GUT-domain/scene-held targets cold and the refusal is disclosed as `worker_channel.override_refused=true` with named route reasons; `GQT_MUTATION_WORKER=0` opts out everywhere |
| `--godot-bin <path>` | omitted | Per-command Godot binary override; priority flag > env > PATH |
