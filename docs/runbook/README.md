# gqt runbook

Per-face usage guides for the `gqt` CLI. Each face keeps typical examples up
front and a full command reference at the end.

| Face | When to enter | File |
|---|---|---|
| `gherkin` | `.feature` → versioned IR → generated GUT tests → run / contract-mutate | [gherkin.md](gherkin.md) |
| `mutation` | Mutation-test `.gd` files (selection, kill verdicts, diff resume, single-site reruns) | [mutation.md](mutation.md) |
| `measure` chain | Build a measurement suite, run GUT coverage (acceptance paused by default), diagnose features, increment attribution maps | [measure.md](measure.md) |
| CRAP risk | Rank functions by CRAP score; coverage and complexity as inputs | [crap.md](crap.md) |
| Guards & doctor | Architecture guard, CC parity, registry diffs, install self-check | [guards.md](guards.md) |

## Command tree

```text
gqt
├─ gherkin compile / run / mutate
├─ mutation
├─ measure suite / run / diagnose / map-update / coverage / complexity / arch / parity / registry-diff / risk
└─ doctor
```

Old top-level commands (`accept`, `arch`, `attr-update`, `canonical`,
`canonical-diagnose`, `complexity`, `coverage`, `parity`, `recipe`,
`registry diff`, `risk`) are gone with no compatibility layer — calling one
exits `2`, with no aliases or deprecation warnings.

## Global invocation

```bash
gqt <command> [args]    # equivalent entry point: python3 -m godot_qa_toolkit.cli <command> [args]
```

Every command prints exactly one JSON object on **stdout**
(`ensure_ascii=False`, indent 2):

```json
{"tool": "<gherkin|mutation|measure.<leaf>>", "ok": true, "summary": {}, "failures": []}
```

| exit | meaning | trigger |
|---|---|---|
| `0` | verdict pass (`ok=true`) | any command |
| `1` | verdict fail (`ok=false`, stdout has JSON) | verdict fail / run_error abort / missing target or GUT runner |
| `2` | usage/loading error, **no stdout JSON** | argparse argument error; `doctor` install mismatch |

Automation should branch only on `0` vs non-zero, then parse the stdout JSON
to distinguish failure shapes. Missing JSON means the tool crashed — treat it
as untrustworthy, not as a verdict fail. `gqt doctor` is the exception: its
self-check JSON goes to **stderr** and stdout stays empty.

**`--project-root`** is the only entry point for the target project; the tool
never discovers or guesses structure. GUT-driven commands require
`<root>/addons/gut/gut_cmdln.gd`; the measurement chain requires
`.gutconfig.json` and `addons/gqt_gherkin/features/`.

**Godot binary**: commands that launch Godot resolve it as
**`--godot-bin` > `GQT_GODOT_BIN` > PATH**. An unusable explicit path becomes a
`BinaryConfigError` (`ok=false` JSON, exit 1, no traceback).
`gherkin compile`, `measure suite/arch/complexity/parity/registry-diff` and
`doctor` never launch Godot.
