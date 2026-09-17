# godot-qa-toolkit

Deterministic five-stage QA toolchain for Godot / GDScript projects
(SEE-1256 → SEE-1268). Every tool emits a machine-decidable verdict under the
unified JSON contract:

```json
{"tool": "...", "ok": true, "summary": {...}, "failures": [...]}
```

Exit code `0` = pass, `1` = fail. Judgments are deterministic (P0' principle);
no agent/heuristic scoring inside tool code.

## Tools

| Tool | Command | Status |
|---|---|---|
| Gherkin runner | `gqt gherkin <file.feature> [--steps module.py]` | M1 |
| Complexity gate | `gqt complexity <paths...> [--warn 10] [--max 15]` | M1 |
| Mutation runner | — | M2 |
| Coverage | — | M2 |
| Godot determinism | — | M2+ (logic/scheduling here; the deterministic commands register into the godot-mcp ① runtime addon) |

> The godot-mcp control plane (launcher/proxy/shim/port arbitration) belongs to
> the **godot-mcp runtime library** (①), not here. This library is the
> tool-state half of a two-library split: run-time driving (①) vs test means
> (②, this repo).

## Gherkin (self-authored)

Minimal, strict Gherkin subset: `Feature`, `Background`, `Scenario`,
`Scenario Outline`, tags (`@tag`), and `Given/When/Then/And/But` steps.
`And`/`But` inherit the previous keyword. Anything unparseable is an error
with `path:line` context — a spec that only "mostly" parses is not
machine-checkable.

Steps bind to a Python registry:

```python
# steps.py
from godot_qa_toolkit.gherkin.runner import Registry

def register(r: Registry):
    @r.step("{n} hearts")
    def hearts(ctx, n):
        ctx["hearts"] = int(n)
```

```bash
gqt gherkin features/leaderboard.feature --steps steps.py
```

## Complexity gate (via gdtoolkit)

Function-level cyclomatic complexity through gdtoolkit's parser + radon.
Files gd2py cannot convert (GDScript identifiers colliding with Python
keywords) are reported as structural failures — unmeasured code is a fail
signal, never a silent skip.

```bash
gqt complexity src/systems --warn 8 --max 12
```

## Development

```bash
pip install -e .
pytest
```

### PYTHONPATH convention (multi-workdir safety, SEE-1308 §SPEC-003)

When several KOL worktrees live on the same machine and share one Python
site-packages, a `pip install -e .` from an older checkout leaves an
`__editable__.godot_qa_toolkit-0.1.0.pth` egg-link that points at that old
tree. pytest then resolves `import godot_qa_toolkit` against the stale
source and can fail spuriously even though the current tree is fine.

Two belts guard against this:

1. **`--import-mode=importlib`** — already set in `pyproject.toml` under
   `[tool.pytest.ini_options].addopts`. It makes pytest resolve the package by
   module path rather than trusting the egg-link, so a wrong-root egg-link
   cannot break the suite's own import.
2. **Explicit `PYTHONPATH`** — when you must run against a specific tree, pin
   it instead of relying on the editable install:

   ```bash
   cd <this-worktree>/.dev/qa-toolkit
   PYTHONPATH="$PWD/src" python -m pytest tests/ -q
   ```

   Re-point a drifting editable install from the *current* tree:

   ```bash
   cd <this-worktree>/.dev/qa-toolkit && pip install -e .
   ```

The rule: never rely on an editable install whose egg-link you did not just
create from the tree you are testing.
