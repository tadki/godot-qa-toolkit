<div align="center">

# godot-qa-toolkit

**Deterministic QA toolchain for Godot / GDScript projects — machine-decidable
verdicts under one unified JSON contract.**

[![ci-pytest](https://github.com/tadki/godot-qa-toolkit/actions/workflows/ci-pytest.yml/badge.svg)](https://github.com/tadki/godot-qa-toolkit/actions/workflows/ci-pytest.yml)
![python](https://img.shields.io/badge/python-3.10%2B-blue)

</div>

---

gqt is an independent CLI (`gqt`) that answers *"does this project pass?"* with
a verdict a machine can act on — no agent scoring, no heuristics inside tool
code. It drives the **tested project's own GUT suite** headlessly, emits exactly
one JSON object per command, and never guesses at your project layout: the
target project enters only through an explicit `--project-root`.

## Why gqt

- **One JSON contract, deterministic verdicts.** Every command prints
  `{"tool", "ok", "summary", "failures"}` on stdout; exit `0` = pass, `1` =
  fail, `2` = caller error. Unmeasured code is a fail signal, never a silent
  skip — agents and CI consume verdicts with zero tool-specific glue.
- **Gherkin acceptance pipeline.** `.feature` files compile to a versioned IR
  and generated GUT tests, then run and contract-mutate inside your project.
  A strict Gherkin subset, no steps registry — tests call your project's
  `testing_api.gd` adapter, and full-matrix mutation proves the assertions
  actually bite.
- **Mutation testing that picks the right tests.** Test selection comes from
  the runtime truth map (`test-attribution/5`): function-level grouping by
  default, a bounded file-level fallback, a terminal zero-reference
  short-circuit, and a loud failure when the map is unusable — the full face
  only runs under the explicit `--select full` audit switch. Mutants execute
  in disk shadow slots (your real repo is never written), differential resume
  re-runs only unproven verdicts, and kills are judged as newly-failing test
  sets, never by exit codes.
- **Dual-arm measurement.** `measure suite` / `measure run` build a hashed
  suite definition and run two coverage arms (full GUT face + acceptance
  suite) whose line sets are **unioned, never averaged**, into function-level
  CRAP scoring and the attribution map that powers mutation selection.
- **Deterministic static guards.** Cyclomatic-complexity thresholds and a
  Pure Core architecture guard (extends whitelist, no engine API, explicit
  RNG) turn code structure into pass/fail gates.

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
gqt doctor    # verify the editable install points at the tree you expect
```

For a development checkout: `bash scripts/install.sh` pins the system `gqt`
to the current tree — re-run it after every checkout switch, and `gqt doctor`
will tell you when you forgot.

**Prerequisites for the GUT-driven commands** (`mutation`, `measure coverage`,
`measure run`, `gherkin run|mutate`):

- `--project-root` points at a Godot project containing
  `addons/gut/gut_cmdln.gd` — gqt consumes the tested project's own GUT and
  ships zero Godot assets itself.
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

# Mutation testing (default: function-level truth-map selection)
gqt mutation src/save/save_manager.gd --project-root . --budget 50

# Single file across a worker pool (default pool = max(2, cpu//2))
gqt mutation <file> --project-root . --jobs 8

# Line + branch coverage with a minimum gate
gqt measure coverage src/save/save_manager.gd --project-root . --min-percent 80

# Static gates
gqt measure complexity src/systems --warn 10 --max 15
gqt measure arch src/systems

# Dual-arm measurement → CRAP + attribution map
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

| Document | What it covers |
|---|---|
| [`docs/contract.md`](docs/contract.md) | **Machine contract SSOT**: per-command flags, JSON fields, sentinel shapes, cache/write boundaries, schema versions |
| [`docs/mutation-architecture.md`](docs/mutation-architecture.md) | Mutation design rationale: disk shadow slots, differential resume, worker channel, freshness proofs |
| [`docs/templates/`](docs/templates/) | Adoption spec, Gherkin gauntlet script, `testing_api.gd` adapter and probe library templates |
| [`docs/new-checker.md`](docs/new-checker.md) | How to add a new checker to the toolchain |

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

- **Deterministic verdicts** — fully reproducible outcomes; no agent or
  heuristic scoring inside tool code.
- **One JSON contract** — uniform `{tool, ok, summary, failures}` and
  `0`/`1`/`2` exits across the toolchain.
- **Unmeasured is a fail** — unconvertible code, unscanned targets and silent
  skips are structural failures, never swallowed.
- **Honest degradation** — caches, locks and resume stores degrade to safe,
  explicitly-disclosed behavior instead of wrong answers.
- **Zero discovery** — no environment sniffing, no project-structure
  inference; the caller wires everything explicitly.
