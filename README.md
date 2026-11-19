<div align="center">

# godot-qa-toolkit

**QA toolchain for Godot / GDScript — one machine-readable JSON verdict per command.**

[![ci-pytest](https://github.com/tadki/godot-qa-toolkit/actions/workflows/ci-pytest.yml/badge.svg)](https://github.com/tadki/godot-qa-toolkit/actions/workflows/ci-pytest.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)

</div>

---

`gqt` is an independent CLI that answers *"does this project pass, and where is
the risk?"* with verdicts a machine can act on. It drives the **tested
project's own GUT suite** headlessly and emits exactly one JSON object per
command. An explicit `--project-root` wins; when omitted, mutation and
measurement commands discover the nearest `project.godot` by walking up from
the target path or CWD, and fail loudly if none exists.

## Why gqt

- **CRAP risk scoring, first class.** Rank functions by Change Risk
  Anti-Patterns — `CRAP = cc² × (1 − c)³ + cc`, with `c` the raw 0–1 line
  coverage (never pre-rounded). `score ≥ 30` or `cc ≥ 15` marks a function as
  high risk; no valid coverage denominator means no score, never a fake one.
  CRAP is a ranking and governance signal, not a hidden CI gate.
- **One JSON contract.** Every command prints `{"tool", "ok", "summary",
  "failures"}` on stdout; exit `0` = pass, `1` = fail, `2` = caller error.
  Unmeasured code is a fail signal, never a silent skip.
- **Gherkin acceptance pipeline.** `.feature` files compile to a versioned IR
  and generated GUT tests, then run and contract-mutate inside your project —
  full-matrix mutation proves the assertions actually bite. No steps registry;
  tests call your project's `testing_api.gd` adapter.
- **Mutation testing that picks the right tests.** Line-level runtime facts are
  the primary truth: each mutant uses the tests that executed its source line,
  and sites with identical test sets share one generated test glob and baseline.
  Explicit line ranges and stable-id reruns use the same line-level mapping;
  function/file views remain explicit calibration and fallback options. An
  unusable map fails loudly, while the full face runs only under `--select
  full`. Mutants execute in disk shadow slots (your real repo is never written),
  differential resume re-runs only unproven verdicts, and kills are judged as
  newly-failing test sets, never by exit codes.
- **Dual-arm measurement, one instrumentation pass.** `measure suite` builds
  the hashed suite definition; `measure run` executes the full GUT and
  acceptance arms whose line sets are **unioned, never averaged**. With
  attribution enabled, the same run records per-test/per-script covered lines,
  derives file/function views, and lets mutation project the matching Coverage
  Store without a second instrumentation or GUT pass.
- **Guards and self-checks.** A Pure Core architecture guard, CC parity,
  registry snapshot diffs, and `gqt doctor` for editable-install consistency.

## The CLI

Four top-level entries — the whole surface fits on one screen:

```text
gqt
├─ gherkin compile / run / mutate          # Gherkin acceptance pipeline
├─ mutation                                # mutation testing (truth-map selection)
├─ measure suite / run / diagnose / map-update
│           coverage / complexity / arch
│           parity / registry-diff / risk  # measurement & diagnostic lanes
└─ doctor                                  # editable-install self-check
```

## Quick start

Install from a release tag (no PyPI channel):

```bash
pip install "git+https://github.com/tadki/godot-qa-toolkit.git@v0.3.0"
gqt --help     # sanity-check: exactly four top-level entries
```

For a development checkout: `bash scripts/install.sh` pins the system `gqt`
to the current tree as an editable install — re-run it after every checkout
switch. In that editable setup, `gqt doctor` verifies the install still
points at the tree you expect (a standalone install with no toolkit tree in
sight is skipped gracefully, not reported as an error).

**Prerequisites for the GUT-driven commands** (`mutation`, `measure coverage`,
`measure run`, `gherkin run|mutate`):

- The project root contains `addons/gut/gut_cmdln.gd`. Pass `--project-root`
  explicitly for an unambiguous target; otherwise mutation and measurement
  commands discover the nearest `project.godot` from the target path or CWD.
  A fresh real Godot project with an incomplete `.godot` cache is automatically
  imported once; set `GQT_AUTO_IMPORT=0` to disable that bootstrap.
- A resolvable Godot binary: per-command `--godot-bin` wins, then
  `GQT_GODOT_BIN`, then the first `godot` on `PATH`.
- For the Gherkin pipeline: the thin adapter at
  `res://addons/gqt_accept/harness.gd` implementing
  [`docs/templates/testing_api.gd.md`](docs/templates/testing_api.gd.md)
  (one-time wiring, no auto-discovery).

Typical calls:

```bash
# Gherkin acceptance: compile, run, contract-mutate
gqt gherkin compile acceptance/foo.feature --project-root . --write
gqt gherkin run    --project-root . --key foo
gqt gherkin mutate --project-root . --key foo

# Mutation testing (default: line-level truth-map selection)
gqt mutation src/save/save_manager.gd --project-root . --budget 50

# Changed .gd files from git diff HEAD, or compact line-level addressing
gqt mutation --project-root .
gqt mutation src/save/save_manager.gd#10-20,30 --project-root .

# Caliper rerun by line range / multi-segment range / stable mutant id
gqt mutation src/save/save_manager.gd --project-root . --lines 10-20,30,35-40
gqt mutation src/save/save_manager.gd --project-root . \
  --mutation "src/save/save_manager.gd#func:save#2"

# Single file across a worker pool (default pool = max(2, cpu//2))
gqt mutation <file> --project-root . --jobs 8

# Line coverage with a minimum gate (branch coverage is observational)
gqt measure coverage src/save/save_manager.gd --project-root . --min-percent 80

# Static complexity measurements and architecture guard
gqt measure complexity src/systems --warn 10 --max 15
gqt measure arch src/systems

# CRAP risk ranking for candidate files
gqt measure risk src/save/save_manager.gd --project-root . --report shadow-report.json

# Dual-arm measurement → CRAP records + attribution map
gqt measure suite --project-root . --out recipe.json
gqt measure run autoload systems --project-root . --recipe recipe.json --out report.json
```

## Output & exit codes

One JSON object on stdout per command (`doctor` is the documented exception:
its self-check JSON goes to stderr):

| exit | meaning | stdout |
|---|---|---|
| `0` | verdict pass (`ok=true`) | JSON |
| `1` | verdict fail (`ok=false`): target failed, unmeasured, or aborted run | JSON |
| `2` | **caller error**: bad arguments (argparse) / `doctor` install mismatch | none (stderr only) |

Branch only on `0` vs `!= 0`, then parse stdout to distinguish failure shapes.
Missing JSON means the tool itself crashed — treat it as untrustworthy, not
as a verdict fail.

## Documentation

- **[Runbook](docs/runbook/README.md)** — per-face usage guides with typical
  examples and full command references (gherkin / mutation / measure / CRAP /
  guards & doctor).
- **[`docs/contract.md`](docs/contract.md)** — machine contract source of
  truth: per-command flags, JSON fields, sentinel shapes, cache and write
  boundaries, schema versions.
- **[`docs/mutation-architecture.md`](docs/mutation-architecture.md)** —
  mutation design rationale: disk shadow slots, differential resume, worker
  channel, freshness proofs.
- **[`docs/templates/`](docs/templates/)** — adoption spec, Gherkin gauntlet
  script, `testing_api.gd` adapter and probe library templates.
- **[`docs/new-checker.md`](docs/new-checker.md)** — how to add a new checker
  to the toolchain.

Upgrading from v0.2.x? v0.3.0 collapsed the CLI into the four entries above
with **no compatibility layer** — old top-level commands exit `2`. The rename
map is in [`docs/contract.md`](docs/contract.md) §2.

## Versioning

Consumers pin by **git tag**; `pyproject.toml` `version` is the single source
of truth and must equal the tag (currently `0.3.0` ↔ `v0.3.0`). Below `1.0` a
minor bump may carry breaking CLI/contract changes (`v0.2.0` and `v0.3.0`
both did); from `1.0` on, breaking changes require a major.

## Development

```bash
bash scripts/install.sh     # pips this tree (PEP 668 retry included)
pytest                      # full suite (PYTHONPATH=src for a pinned tree)
```

With several worktrees on one machine, run tests against the exact tree you
mean: `PYTHONPATH="$PWD/src" python -m pytest tests/ -q`. pytest is configured
with `--import-mode=importlib` so a stale editable egg-link cannot hijack the
suite's own imports.

## Design principles

- **Machine-decidable outcomes** — no agent or heuristic scoring inside tool
  code; every verdict is reproducible from the inputs.
- **One JSON contract** — uniform `{tool, ok, summary, failures}` and
  `0`/`1`/`2` exits across the toolchain.
- **Unmeasured is a fail** — unconvertible code, unscanned targets and silent
  skips are structural failures, never swallowed.
- **Honest degradation** — caches, locks and resume stores degrade to safe,
  explicitly-disclosed behavior instead of wrong answers.
- **Bounded discovery** — an explicit `--project-root` wins; otherwise the
  nearest `project.godot` is discovered by walking up from the target/CWD.
  Invalid explicit roots and discovery failures are loud; unrelated directory
  or repository layouts are never guessed.
