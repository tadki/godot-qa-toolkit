# Guards & diagnostics — arch / parity / registry-diff / doctor

## When to use

- `measure arch`: turn the Pure Core boundary into a machine gate (keep engine
  API / scene-tree access out of pure logic).
- `measure parity`: check whether the two CC collection pipelines (oracle
  registry vs radon) agree function by function, to locate measurement drift.
- `measure registry-diff`: compare two registry snapshots to govern net function
  add/remove/edit outcomes.
- `doctor`: confirm that `gqt`'s editable install points at the current
  worktree.

## Prerequisites

- arch / parity / registry-diff: only source paths; no Godot, no GUT.
- Cross-snapshot comparisons require both snapshots to come from the same
  collection toolchain (same gqt version).
- doctor: applies to editable installs; standalone installs are skipped
  gracefully (no false reports).

## Typical examples

```bash
# Pure Core architecture guard (rules v1: extends whitelist /
# no engine API / no bare global random)
gqt measure arch src/core

# CC parity + registry snapshot (diagnostic-only)
gqt measure parity autoload systems --project-root . --report parity.json --registry-out reg.json

# Registry snapshot diff (governance net-benefit criterion)
gqt measure registry-diff old-reg.json new-reg.json --out diff.json

# Install consistency self-check (self-check JSON goes to stderr; stdout empty)
gqt doctor
GQT_TOOLKIT_ROOT=~/tools/godot-qa-toolkit gqt doctor
```

## Output & exit codes

- arch: `tool=measure.arch`; `summary.{files, violations, unparseable, by_rule}`;
  `failures` = violations (file/line/rule/message) + unparseable.
  `ok = no violations ∧ no unparseable`; exit `0`/`1`.
- parity: a `parity/1` report plus `func-registry-snapshot/1`; seven lanes
  `match / lambda_gap / lambda_disclosed / missing_in_radon /
  identity_mismatch / mismatch / cc_unmeasured`; closure invariant = the seven
  lanes' row counts sum to `func_entries + lambda_entries`
  (`summary.closure_ok`).
- registry-diff: `func-registry-diff/1`, statuses `unchanged / edited / added /
  removed / identity_changed_candidate / removed_identity_candidate`; scope
  escapes (`escape_dir / lambda_kind / moved_into_inner_class`) are counted
  separately and **never counted into governance net benefit** — the machine
  gate is `governance_net.countable_added == added - scope_escape`.
- doctor: one JSON on **stderr** (`{"status":"ok"|"mismatch", …}`; on mismatch
  it includes `editable_location` / `toolkit_root` / `gitlink_head` /
  `fix_hint`); exit `0` = consistent, `2` = install mismatch (no stdout JSON).

## Failure semantics

- Unparseable files in arch are structural failures (unmeasured code = fail
  signal), never silently skipped.
- parity's `identity_mismatch` (same-name count mismatch or extra radon
  entries) is the fail-loud lane; `mismatch` (any other delta) means a
  second-layer conversion defect.
- registry-diff's `removed∩added` with identical body_hash is only a machine
  candidate (`human_final:true`), never auto-counted as benefit.
- On doctor mismatch, re-run `scripts/install.sh` per `fix_hint`; do not edit
  site-packages by hand.

## Boundaries

- parity / registry-diff are diagnostic-only: `ok` only means the command
  completed; lane anomalies are data, not exit codes — no CI wiring, no
  thresholds, no averaging.
- arch rule ids form an open set (three rules in v1, more may come); filter
  with tolerance for unknown rule names.
- doctor judges install consistency, not project quality; it never emits a
  verdict JSON.

## Command reference (aligned with v0.3.0 `--help`)

### measure arch

`gqt measure arch <paths...>`

| Argument | Default | Meaning |
|---|---|---|
| `<paths...>` (positional) | required | One or more `.gd` files or directories to scan (the scan set is passed explicitly by the caller; `addons/` is outside the default scan surface) |

No other options; rules v1 = `core/extends-whitelist` / `no-engine-api` /
`no-bare-global-random`.

### measure parity

`gqt measure parity <paths...> --project-root <root> [--report parity.json] [--registry-out reg.json]`

| Argument | Default | Meaning |
|---|---|---|
| `<paths...>` (positional) | required | Directories or `.gd` files to scan (same roots as the complexity gate) |
| `--project-root <root>` | required | Project root for relative fids |
| `--report <path>` | `./parity.json` | Parity report output path |
| `--registry-out <path>` | omitted | Optional multi-file registry snapshot output path |

### measure registry-diff

`gqt measure registry-diff <old.json> <new.json> [--out diff.json]`

| Argument | Default | Meaning |
|---|---|---|
| `<old.json>` (positional) | required | Old registry snapshot path |
| `<new.json>` (positional) | required | New registry snapshot path |
| `--out <path>` | omitted | Diff JSON output path |

### doctor

`gqt doctor` (no arguments)

Self-checks the "editable install vs current worktree gitlink" consistency.
The self-check JSON goes to **stderr** and stdout stays empty; exit `0` =
consistent, `2` = install mismatch (with a `fix_hint`). The `GQT_TOOLKIT_ROOT`
environment variable selects the checkout to compare against.
