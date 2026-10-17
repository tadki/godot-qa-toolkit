"""Gherkin .feature parser (SEE-1268 M1; SEE-1426 P2 schema 冻结扩展).

Deliberately self-contained: the Owner ruling (SEE-1256) rejected forking
Akdr/GodotGherkin, so we own a minimal, fully specified grammar covering the
subset the five-stage workflow needs — Feature, Background, Scenario,
Scenario Outline, Given/When/Then/And/But steps, tags, and (SEE-1426)
Scenario Outline Examples tables as first-class citizens with the `# types:`
declaration line.

SEE-1426 schema 冻结五件套（§3.3，全部 fail-fast parse error）：
- Examples 表一等公民：表宽不齐 / 占位符无对应列 / Outline 无 Examples
  均在 parse 期报错。
- 占位符归一化：编译轨/IR 只认 `<x>`；Examples 展开后残留 `{x}` = parse
  error（`{x}` 是运行时 runner 私有语法）；非 Outline 场景出现 `<x>` =
  parse error。
- `# types:` 注释行：`# types: <hp>:int, <left>:numeric, <mode>:enum{a,b}`
  声明占位符类型；同名两处冲突 = parse error；出现在无 Outline 的
  Feature 内 / 列名与 Examples 表头不匹配 = parse error；Then 绑定处
  numeric 形 cell 无类型声明 = parse error。
- unchanged 反向断言槽：`# unchanged: <e>.<f>, ...` 注释行声明本场景
  不可被污染的状态路径（harness 基线快照比对）。

Not a general BDD engine: unknown keywords are a parse ERROR (fail fast,
P0' — a spec that only parses "mostly" is not machine-checkable).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STEP_KEYWORDS = ("Given", "When", "Then", "And", "But")
BLOCK_KEYWORDS = ("Feature:", "Background:", "Scenario:", "Scenario Outline:")

TYPES_PREFIX = "# types:"
UNCHANGED_PREFIX = "# unchanged:"

# 编译轨唯一占位符形态：<name>（{x} 属运行时 runner 私有语法）。
_PLACEHOLDER_RE = re.compile(r"<([A-Za-z_]\w*)>")
_RUNTIME_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# Then 步骤里数值形 cell 的判定：纯整数/小数（含负号）视为 numeric 绑定，
# 无 `# types:` 声明即 parse error（fail-fast，非披露档）。
_NUMERIC_CELL_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


class GherkinSyntaxError(ValueError):
    """Raised on malformed .feature input with file/line context."""

    def __init__(self, path: str, line_no: int, message: str):
        super().__init__(f"{path}:{line_no}: {message}")
        self.path = path
        self.line_no = line_no


@dataclass
class Step:
    keyword: str  # canonical: Given/When/Then (And/But inherit the prior kind)
    text: str
    line: int


@dataclass
class ExampleRow:
    values: dict[str, str]  # column name -> cell text
    line: int


@dataclass
class Scenario:
    name: str
    line: int
    tags: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    outline: bool = False
    examples: list[ExampleRow] = field(default_factory=list)
    example_columns: list[str] = field(default_factory=list)
    types: dict[str, str] = field(default_factory=dict)  # <name> -> type spec
    unchanged: list[str] = field(default_factory=list)   # "<entity>.<field>"

    def expansions(self) -> list[tuple[int, dict[str, str], list[Step], dict[str, str]]]:
        """Outline → 具体场景列表：(row_line, values, expanded_steps, types)。

        普通 Scenario 返回单条无替换展开。展开在 parse 侧完成后，下游
        （IR/生成器）拿到的就是零占位符的具体步骤。
        """
        if not self.outline:
            return [(self.line, {}, list(self.steps), dict(self.types))]
        out = []
        for row in self.examples:
            steps = [
                Step(keyword=s.keyword, text=_expand(s.text, row.values), line=s.line)
                for s in self.steps
            ]
            out.append((row.line, dict(row.values), steps, dict(self.types)))
        return out


@dataclass
class Feature:
    name: str
    line: int
    tags: list[str] = field(default_factory=list)
    background: list[Step] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)
    types: dict[str, str] = field(default_factory=dict)  # feature-level `# types:`


def _strip_comment_keep_directives(line: str) -> str:
    """去普通注释，但保留 gqt 指令注释行（# types: / # unchanged:）原样
    返回（调用方按指令解析）；标准 Gherkin 工具链把两行都当普通注释，
    零干扰。"""
    s = line.strip()
    if s.startswith(TYPES_PREFIX) or s.startswith(UNCHANGED_PREFIX):
        return s
    if s.startswith("#"):
        return ""
    return line.rstrip()


def _parse_tags(raw: str, path: str, line_no: int) -> list[str]:
    tags = []
    for token in raw.split():
        if not token.startswith("@"):
            raise GherkinSyntaxError(path, line_no, f"invalid tag {token!r} (must start with @)")
        tags.append(token[1:])
    return tags


def _parse_scenario_header(stripped: str, tags: list[str], line_no: int) -> Scenario | None:
    if stripped.startswith("Scenario Outline:"):
        keyword = "Scenario Outline:"
    elif stripped.startswith("Scenario:"):
        keyword = "Scenario:"
    else:
        return None
    return Scenario(
        name=stripped[len(keyword):].strip(),
        line=line_no,
        tags=tags,
        outline=keyword == "Scenario Outline:",
    )


def _parse_table_row(stripped: str) -> list[str]:
    cells = [c.strip() for c in stripped.strip().strip("|").split("|")]
    return cells


def _expand(text: str, values: dict[str, str]) -> str:
    """展开期替换 <x> → cell 值。展开后残留 {x} = 调用方 parse error
    （在此集中执行保证归一化单点）。"""
    out = _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)
    if _RUNTIME_PLACEHOLDER_RE.search(out):
        raise ValueError(out)  # 由 _validate_expansion 转成带位置的错误
    return out


def _parse_types_line(stripped: str, path: str, line_no: int) -> dict[str, str]:
    """`# types: <hp>:int, <mode>:enum{normal,stun}` → {hp: int, mode: enum{...}}"""
    body = stripped[len(TYPES_PREFIX):].strip()
    if not body:
        raise GherkinSyntaxError(path, line_no, "# types: line declares nothing")
    types: dict[str, str] = {}
    # enum 域声明内含逗号，先按 enum{...} 原子提取再按逗号切分。
    enum_spans = list(re.finditer(r"<(\w+)>:enum\{[^}]*\}", body))
    consumed: list[tuple[int, int]] = [(m.start(), m.end()) for m in enum_spans]
    for m in enum_spans:
        types[m.group(1)] = m.group(0).split(":", 1)[1]
    pieces: list[str] = []
    pos = 0
    for start, end in sorted(consumed):
        pieces.extend(body[pos:start].split(","))
        pos = end
    pieces.extend(body[pos:].split(","))
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        m = re.fullmatch(r"<(\w+)>:(\w+)", piece)
        if m is None:
            raise GherkinSyntaxError(path, line_no,
                                     f"malformed # types: declaration {piece!r} "
                                     "(expected <name>:type or <name>:enum{...})")
        types[m.group(1)] = m.group(2)
    return types


def _check_type_conflicts(existing: dict[str, str], new: dict[str, str],
                          path: str, line_no: int) -> None:
    for name, spec in new.items():
        if name in existing and existing[name] != spec:
            raise GherkinSyntaxError(
                path, line_no,
                f"conflicting type declarations for <{name}>: "
                f"{existing[name]!r} vs {spec!r}")


def _append_step(
    stripped: str,
    first_word: str,
    current_steps: list[Step] | None,
    last_kind: str | None,
    path: str,
    line_no: int,
) -> str:
    if current_steps is None:
        raise GherkinSyntaxError(path, line_no, f"step outside Scenario/Background — {stripped!r}")
    if first_word in ("And", "But"):
        if last_kind is None:
            raise GherkinSyntaxError(path, line_no, f"{first_word} before any Given/When/Then")
        kind = last_kind
    else:
        kind = first_word
    text_part = stripped[len(first_word):].strip()
    current_steps.append(Step(keyword=kind, text=text_part, line=line_no))
    return kind


def _validate_scenario(feature: Feature, scenario: Scenario,
                       path: str, line_no: int) -> None:
    """Schema 冻结校验（parse 期 fail-fast，per-scenario 展开前后各一道）。"""
    # 非 Outline 场景出现 <x> = parse error（编译轨只认 Outline 展开）。
    if not scenario.outline:
        for step in scenario.steps:
            # §SPEC-1426-22：非 Outline 步骤文本的 {x} 残留同样 parse
            # error——编译轨/IR 只认 <x>，{x} 是运行时 runner 私有语法，
            # 不许带字面 {x} 静默进 IR。
            bm = _RUNTIME_PLACEHOLDER_RE.search(step.text)
            if bm:
                raise GherkinSyntaxError(
                    path, step.line,
                    f"runtime placeholder {{{bm.group(1)}}} in non-Outline "
                    f"scenario {scenario.name!r} — compile track only accepts <x>")
            m = _PLACEHOLDER_RE.search(step.text)
            if m:
                raise GherkinSyntaxError(
                    path, step.line,
                    f"placeholder <{m.group(1)}> in "
                    f"non-Outline scenario {scenario.name!r} — use a Scenario Outline")
        return
    if not scenario.examples:
        raise GherkinSyntaxError(path, line_no,
                                 f"Scenario Outline {scenario.name!r} has no Examples table")
    for row in scenario.examples:
        for step in scenario.steps:
            missing = [m.group(1) for m in _PLACEHOLDER_RE.finditer(step.text)
                       if m.group(1) not in row.values]
            if missing:
                raise GherkinSyntaxError(
                    path, step.line,
                    f"placeholder(s) {missing} have no matching Examples column "
                    f"in scenario {scenario.name!r}")
            # 展开后残留 {x} = parse error（{x} 是运行时 runner 私有语法，
            # 编译轨/IR 拒绝）。
            try:
                _expand(step.text, row.values)
            except ValueError:
                raise GherkinSyntaxError(
                    path, step.line,
                    f"runtime placeholder {{x}} survives Examples expansion — "
                    f"compile track only accepts <x>: {step.text!r}") from None
    # Then 绑定 numeric 形 cell 无类型声明 = parse error。
    for row in scenario.examples:
        for step in scenario.steps:
            if step.keyword != "Then":
                continue
            for m in _PLACEHOLDER_RE.finditer(step.text):
                name = m.group(1)
                cell = row.values.get(name, "")
                if (_NUMERIC_CELL_RE.match(cell) and name not in scenario.types
                        and name not in feature.types):
                    raise GherkinSyntaxError(
                        path, step.line,
                        f"Then binds numeric placeholder <{name}> (cell {cell!r}) "
                        f"with no # types: declaration")


def _apply_block_line(
    feature: Feature,
    stripped: str,
    current_scenario: Scenario | None,
    current_steps: list[Step] | None,
    in_examples: bool,
    pending_tags: list[str],
    last_kind: str | None,
    path: str,
    line_no: int,
) -> tuple[list[Step] | None, bool, list[str], str | None, Scenario | None]:
    if stripped.startswith("Background:"):
        # And/But 继承只在本 block 内生效——新 block 头重置（修复跨
        # Scenario/Background 泄漏：上一 block 末尾 Then 会让下一 block
        # 起手的 And 被误判合法，runner 按 keyword 匹配即错路由）。
        return feature.background, False, pending_tags, None, None
    scenario = _parse_scenario_header(stripped, pending_tags, line_no)
    if scenario is not None:
        feature.scenarios.append(scenario)
        return scenario.steps, False, [], None, scenario
    if stripped == "Examples:":
        if current_scenario is None or not current_scenario.outline:
            raise GherkinSyntaxError(
                path, line_no, "Examples: table outside a Scenario Outline")
        if current_scenario.examples:
            raise GherkinSyntaxError(
                path, line_no,
                f"duplicate Examples: table in scenario {current_scenario.name!r}")
        return current_steps, True, pending_tags, last_kind, current_scenario
    if stripped.startswith("|"):
        if in_examples and current_scenario is not None:
            cells = _parse_table_row(stripped)
            if not current_scenario.examples and not current_scenario.example_columns:
                if any(not c for c in cells):
                    raise GherkinSyntaxError(path, line_no, "empty Examples column header")
                # §SPEC-1426-23：重复列名 = parse error（dict(zip(...)) 会
                # 静默让后列覆盖前列，禁止）。
                if len(set(cells)) != len(cells):
                    dupes = sorted({c for c in cells if cells.count(c) > 1})
                    raise GherkinSyntaxError(
                        path, line_no,
                        f"duplicate Examples column(s) {dupes} in scenario "
                        f"{current_scenario.name!r} — later column would silently "
                        f"shadow the earlier one")
                current_scenario.example_columns = cells
            else:
                if len(cells) != len(current_scenario.example_columns):
                    raise GherkinSyntaxError(
                        path, line_no,
                        f"Examples row width {len(cells)} != header width "
                        f"{len(current_scenario.example_columns)} (ragged table)")
                current_scenario.examples.append(ExampleRow(
                    values=dict(zip(current_scenario.example_columns, cells)),
                    line=line_no))
            return current_steps, in_examples, pending_tags, last_kind, current_scenario
        raise GherkinSyntaxError(path, line_no, f"table row outside Examples — {stripped!r}")
    first_word = stripped.split(None, 1)[0]
    if first_word in STEP_KEYWORDS:
        new_kind = _append_step(stripped, first_word, current_steps, last_kind, path, line_no)
        return current_steps, False, pending_tags, new_kind, current_scenario
    raise GherkinSyntaxError(path, line_no, f"unrecognized line — {stripped!r}")


def parse(text: str, path: str = "<feature>") -> Feature:
    """Parse .feature text into a Feature tree. Raises GherkinSyntaxError."""
    feature: Feature | None = None
    current_scenario: Scenario | None = None
    current_steps: list[Step] | None = None  # background or scenario steps
    in_examples = False
    pending_tags: list[str] = []
    last_kind: str | None = None  # for And/But chaining
    pending_types: dict[str, str] = {}
    pending_unchanged: list[str] = []

    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = _strip_comment_keep_directives(raw).strip()
        if not stripped:
            continue

        if stripped.startswith("@"):
            pending_tags.extend(_parse_tags(stripped, path, line_no))
            continue

        # gqt 指令注释：挂到当前（或下一个）scenario / feature。
        if stripped.startswith(TYPES_PREFIX):
            types = _parse_types_line(stripped, path, line_no)
            target = current_scenario if current_scenario is not None else None
            base = target.types if target is not None else feature.types if feature else None
            if base is None:
                raise GherkinSyntaxError(path, line_no, "# types: before Feature:")
            _check_type_conflicts(base, types, path, line_no)
            base.update(types)
            if target is None:
                pending_types.update(types)
            continue
        if stripped.startswith(UNCHANGED_PREFIX):
            body = stripped[len(UNCHANGED_PREFIX):].strip()
            if not body:
                raise GherkinSyntaxError(path, line_no, "# unchanged: declares nothing")
            items = [i.strip() for i in body.split(",")]
            for item in items:
                if not re.fullmatch(r"[A-Za-z_]\w*\.[A-Za-z_]\w*", item):
                    raise GherkinSyntaxError(
                        path, line_no,
                        f"malformed # unchanged: entry {item!r} (expected <entity>.<field>)")
            if current_scenario is None:
                raise GherkinSyntaxError(
                    path, line_no,
                    "# unchanged: outside a Scenario — per-scenario slot only")
            current_scenario.unchanged.extend(items)
            continue

        if stripped.startswith("Feature:"):
            if feature is not None:
                raise GherkinSyntaxError(path, line_no, "duplicate Feature:")
            feature = Feature(
                name=stripped[len("Feature:"):].strip(),
                line=line_no,
                tags=pending_tags,
                types=dict(pending_types),
            )
            pending_types = {}
            pending_tags = []
            continue

        if feature is None:
            raise GherkinSyntaxError(path, line_no, f"line before Feature: — {stripped!r}")

        current_steps, in_examples, pending_tags, last_kind, current_scenario = _apply_block_line(
            feature, stripped, current_scenario, current_steps, in_examples,
            pending_tags, last_kind, path, line_no,
        )
        # scenario 头出现时把此前挂起的 feature 级 types 落进去（已并入
        # feature.types，这里复制引用语义）。
        if (current_scenario is not None and not current_scenario.types
                and feature.types):
            current_scenario.types = dict(feature.types)

    if feature is None:
        raise GherkinSyntaxError(path, 1, "no Feature: line found")

    # `# types:` 出现在无 Outline 的 Feature 内 = parse error（Revy 保留条款）。
    if feature.types and not any(s.outline for s in feature.scenarios):
        raise GherkinSyntaxError(
            path, feature.line,
            "# types: declared in a Feature with no Scenario Outline")
    for s in feature.scenarios:
        _validate_scenario(feature, s, path, s.line)
    return feature
