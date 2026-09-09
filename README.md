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
| MCP control plane | `mcp/` | M3 (migrates from KingOfLikes `.dev/godot-mcp/launch/`) |

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
