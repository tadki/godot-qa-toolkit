<div align="center">

# godot-qa-toolkit

**Deterministic five-stage QA toolchain for Godot / GDScript projects.**

Every tool emits a machine-decidable verdict under one unified JSON contract —
exit code `0` = pass, `1` = fail. No agent scoring, no heuristics inside tool
code: judgments are deterministic by design.

[![ci-pytest](https://github.com/tadki/godot-qa-toolkit/actions/workflows/ci-pytest.yml/badge.svg)](https://github.com/tadki/godot-qa-toolkit/actions/workflows/ci-pytest.yml)
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
| **Arch guard** | `gqt arch <paths...>` — Pure Core architecture guard | shipped |
| **Accept pipeline** | `gqt accept compile <feature> --project-root <root> [--write]` / `gqt accept run [<alt-project-root>] --project-root <root> --key <feature-key>` — Gherkin → IR → generated GUT tests → headless run; the optional positional is an alternative project root (one containing `addons/gut/`), not an artifacts dir | shipped |
| **Complexity gate** | `gqt complexity <paths...> [--warn 10] [--max 15]` | shipped |
| **Mutation runner** | `gqt mutation <file.gd...> --project-root <root> [--budget 50] [--timeout S] [--tests <res://glob>] [--dry-run] [--jobs N] [--no-reuse-coverage] [--worker-channel] [--godot-bin <path>]` | shipped |
| **Coverage runner** | `gqt coverage <file.gd> --project-root <root> [--min-percent 80] [--timeout S] [--godot-bin <path>]` | shipped |
| **Godot determinism** | logic/scheduling here; deterministic commands register into the godot-mcp runtime addon | planned |

`mutation` and `coverage` share three mandatory preconditions:

- `--project-root` must point at a Godot project root containing
  `addons/gut/gut_cmdln.gd` — the tools consume the **tested project's own
  GUT**, they ship zero Godot assets of their own;
- a resolvable Godot binary: per-command `--godot-bin <path>` wins, then the
  `GQT_GODOT_BIN` env var, then the first `godot` on `PATH` (SEE-1525;
  omitting both keeps the historical PATH-only behavior byte-for-byte). The
  `--path` argument handed to godot follows the detected binary kind — win64
  builds get WSL paths converted via `wslpath`, Linux builds get them as-is;
- `--project-root` is the *only* channel through which the test subject
  enters the tool: zero discovery, zero environment guessing, zero writes
  outside the target `.gd` (always restored) and the coverage probe sink
  under `user://`. Relative `<file.gd>` targets are anchored to
  `--project-root` (absolute paths pass through unchanged), so target
  resolution never depends on the caller's CWD (SEE-1576).

> **Two-library split:** the godot-mcp control plane (launcher / proxy / shim /
> port arbitration) belongs to the **godot-mcp runtime library**. This repo is
> the tool-state half: *run-time driving* lives in the runtime library, *test
> means* live here.

## Quick start

```bash
bash scripts/install.sh     # pips this tree (PEP 668 retry included)
pytest                      # run the suite
gqt doctor                  # verify your install + toolchain wiring
```

Then pick a tool:

```bash
# 1. Gherkin acceptance pipeline（承接退役 gherkin runner 的场景判定）
gqt accept compile acceptance/foo.feature --project-root . --write
gqt accept run    --project-root . --key foo
gqt accept mutate --project-root . --key foo

# 2. Gate cyclomatic complexity
gqt complexity src/systems --warn 8 --max 12

# 3. Pure-core arch guard
gqt arch src/systems --warn 8 --max 12

# 4. Mutation-test two scenes in one process, 8 workers
gqt mutation scenes/ui/a.gd scenes/ui/b.gd --project-root . --jobs 8
```

---

`gqt accept` accepts a strict Gherkin subset (`Feature`/`Background`/
`Scenario`/`Scenario Outline`/tags/steps). The standalone `gherkin` command
and the `godot_qa_toolkit.gherkin` module were **physically retired in
SEE-1468 M5-b**: the parser was promoted to `accept/parser.py` and judgement
moved into `gqt accept` (see the Accept pipeline below). The old steps
registry (`gherkin.runner.Registry`) and the `--steps` / per-scenario
`--timeout` flags no longer exist — generated tests are driven by the
target project's `testing_api.gd` adapter instead.

## Accept pipeline

`gqt accept` turns a `.feature` into a versioned IR plus data-driven GUT
tests, then runs and contract-mutates them inside the target project:

```bash
gqt accept compile acceptance/foo.feature --project-root . --write
gqt accept run    --project-root . --key foo
gqt accept mutate --project-root . --key foo
```

**Wiring prerequisite (one-time, per project).** `run` and `mutate` drive the
suite through a thin adapter layer that the tool never discovers on its own:
it must exist at the fixed path `res://addons/gqt_accept/harness.gd` and
implement the interface documented in
[`docs/templates/testing_api.gd.md`](docs/templates/testing_api.gd.md). The
adapter maps the four-step combo (`smoke_check`, `init_case`, `run_step`,
`query_state`) plus `check_expectations` onto the project's real testing API
— detection, inference and tag-based routing are deliberately absent. Without
this file every generated test fails explicitly with
`accept harness unavailable`: run goes red, nothing is skipped silently.

`compile` needs no wiring (it only writes `acceptance/ir` and
`acceptance/generated`), so a project can adopt the pipeline incrementally.
[`docs/templates/accept_gauntlet.sh`](docs/templates/accept_gauntlet.sh)
bundles compile → run → mutate → arch into one gate;
[`docs/templates/adoption_spec.md`](docs/templates/adoption_spec.md) is the
wiring checklist.

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

## Mutation runner

Runs baseline GUT on the untouched source (always measured — it is the
verdict anchor), plans the execution face differentially, then runs every
mutant through an isolated **disk shadow slot** (SEE-1550 default): each
worker owns a hardlink copy-on-write shadow of the project (real copy where
hardlinks are unavailable; `.godot` copied per slot), the mutant lands in the
shadow's target `.gd` via temp-file + atomic `os.replace` *before* the engine
starts, and the real repository is never written. A four-level reachability
beacon (disk read-back hash → load/compile assert → autoload same-source
assert → GUT run evidence) guarantees the engine actually executed the
mutant. The former in-memory injection path is retired — there is no
`--injection` switch.

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
- Per-mutant timeout auto-adapts to `baseline wall-clock × F` (default 2.0,
  override with `--timeout-factor`; only widened, never tightened; visible as
  `summary.adaptive_timeout_s`).
- `--tests <res://glob>` scopes GUT to an affected-test directory;
  `--dry-run` only counts mutation sites (and estimates the differential
  retry face) without running GUT.
- `ok = survived == 0 AND timeout == 0 AND run_errors == 0 AND suspect == 0`
  (`suspect` = cannot prove the mutant was killed — gated same as survived).
- **Mechanism disclosures (SEE-1550, additive only)**: `summary` carries
  `inject_scheme` (always present, `disk-farm/1`), `slot_mode`
  (`fresh`|`warm`), `freshness` (layered freshness verdicts),
  `killed_by_certificates` and `suspected_orphans` when those semantics
  apply.
- **Suspected orphans are disclosed, never skipped**: a target with zero
  static references (autoload wiring / `.tscn` / test literals / other
  script refs) that still has survivors gets each survivor flagged
  `suspected_orphan: true` and listed in `summary.suspected_orphans`;
  statically undecidable faces (dynamic `load`, uid indirection) never
  become a verdict and never skip execution.

### Differential mutation core (on by default)

Once a v2 resume manifest exists, runs are **differential by default**
(scope manifests per top-level func / inner class / module + header hash;
stable ids `<file>#<scope_id>#<n>`): unchanged scopes reuse `killed`
verdicts; `survived`/`timeout`/`timeout_pinned`/`run_error` sites are
**always re-run** (cache is never trusted for unproven kills); a header
(non-func top-level declaration) change re-runs the whole file. `--mutate-all`
forces full execution. Budget applies to the **post-diff retry set** and the
report discloses `retry_total / executed / untouched_ranges / diff_skipped`.

- `# gqt: no-mutate` filters sites (function-level when placed on/above a
  top-level `func`, else line-level).
- A mutant that times out in **2+ consecutive runs** degrades to its own
  `timeout_pinned` verdict: always re-run, visible forever, **not** in the
  ok gate and never merged into `killed`; a poisoned run (≥50% timeouts and
  ≥5 sites) suspends streak accrual outright.
- `--mutation <file>#<scope>#<n>` / `--lines N` re-run single sites in
  seconds (1 baseline + N sites) — the fair spot-check channel; they bypass
  diff reuse and the coverage prefilter.
- `--mutation-warning N` (default 50) warns on pre-truncation site count;
  sites > budget always disclose `total_sites/executed`.
- The coverage prefilter is **on by default** (SEE-1550 L1): sites are
  pre-filtered by the last `gqt coverage` run for the file (own-JSON store,
  provenance- and identity-checked — any drift disables the prefilter with
  a stderr disclosure). Uncovered sites are marked verdict `uncovered`
  (explicit, not in the ok gate, never executed, do not consume budget).
  `--no-reuse-coverage` turns it off; the legacy `--reuse-coverage` flag is
  still accepted (a no-op synonym of the default).
- **CI migration (one-time)**: the SEE-1550 OPS bump (`see1550/1`)
  invalidates pre-existing resume/coverage stores in one shot — they are
  neither read, written, nor deleted (disclosed on stderr) and the first
  run re-executes everything. Diff then changes the red/green surface from
  "every run is full" to "every run re-runs unproven kills".
  `--mutation-warning` / `--timeout-factor` / `timeout_streak` never enter
  any resume key.

### Planning fast paths (SEE-1571, default-on)

- **Zero-reference targets short-circuit instead of falling back to the full
  suite.** When `--tests` is omitted and the derived test glob has **zero**
  references for a target that has executable mutation sites, gqt records an
  intentional non-execution result instead of running every GUT test: the file
  reports `ok=true` with zero verdict counts, `jobs=0`,
  `summary.zero_ref_short_circuit=true`, an explanatory `note`, and a matching
  stderr line. This is a disclosed quality trade-off — potential kills are
  intentionally not executed because no test references the target. Explicit
  `--tests`, `--dry-run`, zero-site targets and collection errors never take
  this short-circuit. In a multi-file run the record stays in `results`; no
  input file disappears silently.
- **Planning reuses one in-process gdself index.** Auto-scan planning builds
  the forward index once per resolved project root and reuses it read-only for
  every target in the same gqt process. The cache is process-local only (zero
  persistence), so a new process always sees current files; planning output is
  identical to rebuilding the index per file.
- **Small targets may auto-select serial execution from measured priors.** At
  the completion of a single-file run, gqt normalizes `stage_timings` into
  `.qa-cache/gqt-mutation-cost-priors.json` (`prewarm_s`, `baseline_s`,
  `per_mutant_s`, `slot_fixed_s`; single writer, atomic replace; zero samples
  keep the old prior). For targets of **at most 8 sites**, gqt compares the
  measured serial and parallel cost models and may choose serial. Targets with
  more than 8 sites are outside the arbitration domain and always keep the
  parallel default — even if the prior file is poisoned by a slow slot. Missing,
  invalid or zero-per-mutant priors also keep parallel. Every arbitration (or
  out-of-domain refusal) is disclosed on stderr with both estimates and the
  four priors.

### Prewarm freshness proof (SEE-1571, default-on)

The parallel path's `godot --headless --import` prewarm is guarded by a content
proof under `.qa-cache/gqt-prewarm/proof.json`. A skip happens only when the
current project input digest **and** the full `.godot` output seal both match
the certificate byte-for-byte; a successful proof is issued only after two
successful imports converge to the same input/output content state. Input
hashing covers all regular project files except `.git/`, `.qa-cache/`, `.godot/`
and the exact JUnit report declared by the current `.gutconfig.json`; `.uid`,
`.import`, unknown extensions and `.gdignore` are all included. Engine identity
binds the launcher plus every wrapper-chain link through the terminal binary by
current SHA-256. `importer="keep"` sidecars are non-import configuration input:
their bytes enter the input digest, they do not join the importer closure, and
having no remap output is valid.

Anything unclear is fail-closed: unknown importers or custom import plugins,
symlinks/non-regular files, unreadable or malformed sidecars, missing remap
outputs, an unresolvable launcher (including a literal bare `godot` command),
a cyclic/too-deep wrapper chain, lock failure, certificate mismatch, non-zero
import rc, `ERROR:` output, or non-converging double imports all mean "no
certificate — import again". Before any baseline consumes the real repo, a
baseline-in-farm slot, or a normal Linux farm slot, gqt re-checks both digests
at that execution root; a mismatch forces reproof/rebuild before baseline
launch.

Set `GQT_PREWARM_SENTINEL=0` (also `false` / `no`) to force the old
always-import behavior. Fresh skips disclose the proof id and both digest
prefixes on stderr; re-imports disclose their concrete reason. Serial runs keep
their existing no-prewarm semantics, so this optimization applies to the
parallel path.

### Disk shadow execution layer & cache identity (SEE-1550)

- **Execution layers by platform**: win64/DrvFS uses a persistent warm slot
  pool under `.qa-cache/mutation-pool/` (built once on first run, then
  per-run `git status` delta sync + targeted relink; pool-ledger + per-slot
  flock against concurrent gqt processes); Linux/ext4 uses a fresh farm
  per run (built and released every run). Either way the mutant only ever
  touches the shadow slot.
- **Freshness authority**: repo-side git delta is taken once per run;
  single-site/caliper runs (`--mutation`/`--lines`) do a targeted
  three-way check (repo stat vs slot stat vs run record) instead of a full
  sweep — this is also what catches SIGKILL-leftover mutants. Any
  ambiguity rebuilds the whole slot. A full census only runs on explicit
  `GQT_MUTATION_FULL_CENSUS=1`.
- **Cache identity package**: resume/coverage keys fold in `inject_scheme`
  (`disk-farm/1`), the exact Godot version string, a boot/wiring hash
  (normalized `project.godot` + autoload manifest + referenced `.tscn`
  manifest), and the GUT addon content hash — any drift means old keys
  never match again (no migration, no dirty merges).
- **Kill certificates**: every killed entry carries the full failing-test
  set (`killed_by`, disclosed via `summary.killed_by_certificates`). A
  cached kill is reused only while every killer test still exists with an
  unchanged script/dependency closure: killer edited/deleted/deps drifted →
  only that entry re-runs; certificate missing/unresolvable → conservative
  re-run; **new tests never invalidate old kills**.

### Worker channel (default-on, SEE-1550 §SPEC-020)

- **The resident worker channel (`worker-reset/1`) is default-on** for
  statically eligible targets — no flag needed. Static routing sends
  autoload / GUT-domain / scene-held targets to the cold channel
  (`cold-shadow/1`), and runtime equivalence gates (parity pre-flight,
  state-drift, canary disqualification) automatically degrade any
  misbehaving target back to cold with stderr disclosure. Opt out with
  `GQT_MUTATION_WORKER=0` (cold; honored on every execution path — serial,
  auto-serial and parallel sharding); `--worker-channel` forces the worker
  regardless of routing for diagnostics.
- **What it does**: eligible targets run in a per-slot persistent
  GUT-direct-drive worker; mutant shadow-disk writes keep the exact M0
  timing, and per-leg state is reset by the R-C protocol (in-place IGNORE +
  replace reload of the closure) under equivalence gates.
- **Static routing decides eligibility** (converged to cold before any
  execution): targets in the `[autoload]` table, reload closures touching an
  autoload member, a scene-cached holder or the GUT domain stay cold —
  machine-readable reason codes (`target_in_autoload_table`,
  `closure_member_in_autoload_table`, `closure_scene_cached`,
  `target_in_gut_domain`, `closure_intersects_gut_domain`) are disclosed
  additively.
- **Equivalence discipline**: first-mutant + periodic dual-channel canaries
  (every `GQT_WORKER_CANARY_EVERY` legs, default 25); any reload failure or
  hash-gate mismatch makes the session fatal — no further mutants in that
  process, the window is invalidated and re-run cold, never silently
  re-routed. `exec_scheme` joins the identity package, so resume stores and
  kill certificates are isolated per channel.
- **Measured (final HEAD, not estimates)**: the equivalence-provable fixture
  suite runs ~1.14× faster on the worker channel; the worker-reachable share
  of a KOL census is ~50% after scene-holder cold routing. win64 keeps the
  worker channel forced cold with disclosure (verdict equivalence holds,
  leg availability does not).
- **Warm reuse needs one channel identity (SEE-1564 D-1)**: the resume
  manifest and the per-mutant records must be written under the *same*
  channel key. The parent planner now resolves each target's scheme through
  the same `identity_for_target` route the runner uses, so a worker-routed
  file's manifest lands on the worker key its records use. Cold keys are
  unchanged (the injection is a no-op there) and A/B channel isolation is
  preserved. Without this, the default worker channel never saw a prior
  manifest and re-ran every mutant on the next run (`no_prior_manifest`).
- **Coverage identity is channel-neutral (SEE-1564 D-2)**: coverage rows are
  a channel-independent fact — line coverage does not depend on the execution
  channel — so the prefilter reads and writes them under the canonical cold
  identity. Comparing them against the caller's channel identity would make
  every worker-routed target look drifted and permanently disable the
  prefilter. Manifest and resume-store keys keep the channel identity, so
  D-1's per-channel isolation is untouched; any *other* identity drift (ops
  version, Godot version, wiring hash, GUT hash) still invalidates the
  prefilter with a stderr disclosure.

### Performance instrumentation & measurement knobs (SEE-1564)

Every non-dry-run report is additively instrumented; no existing field
changes meaning. With every knob unset the only additions are the
measurement fields below — knob disclosure is itself opt-in. The
instrumentation exists to make cost decomposition measurable — it does not
speed anything up by itself.

- **`stage_timings`** — eight cumulative wall-clock buckets, in seconds
  (rounded to 3 decimals): `test_scan`, `prewarm`, `baseline`, `per_mutant`,
  `worker_boot`, `slot_sync`, `prefilter`, `total`. A stage that happens more
  than once (multiple shards/files) is summed. Two disclosure levels:
  - **Unit level** (`summary.stage_timings` of a per-file result): only the
    stages that unit executed itself. `prewarm` is parent-only, so it is
    always `0.0` here; `baseline` is the baseline GUT time this run consumed
    — including a reused parent snapshot's elapsed time (share-attribution
    semantics).
  - **Run level** (the multi-file envelope `stage_timings`): parent
    orchestration stages (`test_scan` / `prewarm` / `baseline` / `prefilter` /
    `total`) plus the summed shard execution stages (`per_mutant` /
    `worker_boot` / `slot_sync`). Same-named stages are deliberately *not*
    merged across layers: a unit's `baseline` is a snapshot time, the
    parent's is the pre-run wall clock.
- **`slot_sync` semantics**: the full window from slot acquisition (including
  fresh provisioning / warm delta sync) through the release assertion. It
  therefore **contains** the whole `per_mutant` span; the slot's fixed cost
  is `slot_sync − per_mutant` (≈0.09 s/chunk measured on the QA fixture).
- **`rss_peak_mb`** — this gqt process's `VmHWM` high-water mark (Linux
  `/proc`; `0.0` when unavailable is an honest default). Godot subprocess
  memory is *not* included — sample it externally in a benchmark.
- **`disk_delta_bytes`** — byte delta of `.qa-cache` plus the ext4 shadow
  farm tree (before vs after). Provided at run level (envelope) and projected
  into the single-file `summary`; never per-shard (tree walks would duplicate
  and the semantics are per-run).
- **Measurement-only knobs** (opt-in; unset = current behaviour exactly).
  They are disclosed only when set — under `knobs` on the envelope, and
  projected into `summary.knobs` for a single file:
  - `GQT_MUTATION_JOBS_CAP` — override the jobs cap. Default
    `max(2, cpu//2)`; the value is still clamped to ≥2. An invalid value
    warns on stderr and falls back to the default.
  - `GQT_MUTATION_POOL_MAX_SLOTS` — override the win64 warm pool's slot cap.
    Default `8`; clamped to ≥1; an invalid value warns and falls back.
  - `GQT_MUTATION_BASELINE_IN_FARM` — run the baseline inside a throwaway
    ext4 shadow slot (fresh slot, never entering the warm pool) instead of
    the real repo, for FS-axis measurement. Default **OFF**: the "real repo
    is the baseline authority" (D5) path is unchanged. If provisioning
    fails, the run degrades back to the real repo with a stderr disclosure
    and discloses `baseline_in_farm: false`.
- **Benchmarks (SEE-1564 §SPEC-201)**: `benchmarks/bench_ab_see1550.py`'s
  `warm` scenario is true zero-execution reuse (no `--mutate-all`, so all
  kills are reused and only the baseline remains);
  `benchmarks/bench_worker_channel.py`'s `kol` mode takes an
  already-prepared project root (`prepared_proj`) and skips fixture
  preparation instead of touching an undefined `fixture`.

### Survivor location in the report

Every verdict record (any `failures` entry; killed records are never listed
there but their rebuilt records share the same shape) carries
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

**Single files go parallel too.** `--jobs` slices a single file
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
- `GQT_PREWARM_SENTINEL=0|false|no` — escape hatch for the default-on prewarm
  freshness proof: always run `godot --headless --import` instead of accepting
  a proof-backed skip.
- `GQT_GODOT_BIN` — global default Godot binary for every subcommand that
  launches godot (`mutation` / `coverage` / `accept run|mutate`); each of
  them also takes a per-command `--godot-bin <path>` override (priority:
  flag > env > PATH, SEE-1525). An invalid explicit path fails loudly with
  the unified JSON error contract instead of silently falling back.
- `GQT_GLOBAL_GODOT_SLOTS` — **cross-process** cap on live godot subprocesses
  (default `max(2, cpu//2)`). Every GUT run (baseline + per-mutant, and
  coverage) takes a flock slot under `.qa-cache/gqt-godot-slots/`; slots are
  released automatically when a process dies. Running two gqt processes in
  parallel? Lower this (or `--jobs`) so the total never exceeds your cores —
  two un-capped processes at 8 workers each = 16 godot instances fighting
  over 16 cores. On platforms without `fcntl` the gate warns once and steps
  aside (never blocks).
- `GQT_MUTATION_POOL_BUDGET_MB` — disk budget of the win64 persistent warm
  slot pool (default 512 MB; slot cap 8; LRU-pruned when exceeded — see
  "Disk shadow execution layer" above). Linux uses a fresh per-run farm,
  so the budget applies to the persistent pool only.
- `GQT_MUTATION_FULL_CENSUS=1` — force a full census of the warm pool;
  by default census runs only for slot validation/ambiguity, and
  single-site runs take the targeted three-way channel instead.
- **Progress (SEE-1525)** — every mutant verdict prints one line to
  **stderr** (`[12/361] killed L123 calc.gd#func:add#1 2.3s`, plus
  `worker=wN` on parallel shards or `worker=serial` on the baseline-fallback
  serial path; `total` is the whole run's execution face).
  The same counter atomically maintains a machine-readable shard under
  `<root>/.qa-cache/gqt-mutation-progress/<run-id>/` (one file **per worker
  process**, aggregated on read — no shared writer, so no write race). Any
  session can poll it without holding the terminal:

  ```bash
  python -c "import json,sys; from godot_qa_toolkit.mutation.progress import read_progress; print(json.dumps(read_progress(sys.argv[1]), ensure_ascii=False, indent=2))" <root>
  ```

  Fields: `done / total / killed / survived / timeout / run_errors / suspect /
  invalid_mutants / elapsed_s / started_at / updated_at`. stdout still carries
  exactly one JSON; progress never touches it. If the parent's shared baseline
  snapshot fails, the file falls back to serial execution and says so on
  stderr (`baseline snapshot failed … falls back to serial …`) instead of
  degrading silently.
- `--resume` — every run already writes each mutant's verdict to the v2
  store `.qa-cache/gqt-mutation-resume-v2/<file-key>/` as it lands. If a
  run is cancelled, rerun the same command with `--resume`: completed
  mutants are skipped and the merged report is identical in shape to a
  full run (`summary.resumed` counts the skips). The file-key is
  `sha1(project-relative path + tests_glob + ops version + binary kind +
  identity digest)` — change any component and the stale verdicts are
  ignored (re-run, never dirty-merged). A corrupt or unreadable store
  degrades honestly to "no history". `--dry-run` stays zero-IO.

Auto-scan note: when a mutation target is referenced by
exactly one test file, the scanner now scopes to that file (GUT `-gselect`)
instead of its directory — same conservative direction (narrower can only
over-report survivors, never fake kills), cheaper per round. The underlying
gdself forward index is built once per gqt process and reused read-only for all
targets in that process; it is never persisted.

</details>

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

### Versioning & release tags

The toolkit has no PyPI channel; consumers pin it by **git tag**:

```bash
pip install "git+https://github.com/tadki/godot-qa-toolkit.git@v0.1.0"
```

- `pyproject.toml` `version` is the single source of truth and **must equal the
  tag** with a `v` prefix (currently `version = "0.1.0"` ↔ tag `v0.1.0`).
- Versioning is semantic: bug-fix batch → patch (`v0.1.1`), backward-compatible
  contract addition → minor (`v0.2.0`), breaking CLI/contract change → major.
- A tag is cut on the merged commit of the release batch (never on an
  unmerged branch tip), and consumers bump their install line to the new tag in
  the same change that needs the new behavior.
- `gqt doctor` catches the "editable install points elsewhere" failure mode;
  tags are what CI and downstream repos consume.

`scripts/install.sh` pins the system `gqt` editable install to **this**
checkout. On a machine with several godot-qa-toolkit trees, a stale editable
egg-link silently makes `gqt` run old tool code — which coverage/mutation
verdicts cannot tolerate. `gqt doctor` compares the editable install location
against the current tree and reports mismatches with a repair hint; re-run
`install.sh` after every checkout switch.

<details>
<summary><strong>PYTHONPATH convention (multi-workdir safety)</strong></summary>

When several worktrees live on the same machine and share one Python
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
<summary><strong>Standalone install mode</strong></summary>

gqt is an independent CLI installed from a standalone checkout (e.g. a
persistent `~/tools/godot-qa-toolkit` on WSL), not a submodule of the tested
project. `gqt doctor` works out of the box in
standalone mode (skips the root comparison when it cannot resolve a target
tree — exit 0, never a false report). To have doctor compare the editable
install against a specific tree, point it there:

```bash
GQT_TOOLKIT_ROOT=~/tools/godot-qa-toolkit gqt doctor
```

</details>

## Design principles

- **Deterministic verdicts** — every tool's outcome is fully
  reproducible; no agent or heuristic scoring inside tool code.
- **One JSON contract** — uniform `{tool, ok, summary, failures}` output and
  `0`/`1` exit codes across the whole toolchain.
- **Unmeasured is a fail** — unconvertible code, unscanned targets and silent
  skips are reported as structural failures, never swallowed.
- **Honest degradation** — caches, locks and resume stores degrade to safe,
  explicitly-reported behavior instead of wrong answers.
