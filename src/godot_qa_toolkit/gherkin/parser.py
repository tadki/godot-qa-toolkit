"""Gherkin .feature parser (SEE-1268 M1).

Deliberately self-contained: the Owner ruling (SEE-1256) rejected forking
Akdr/GodotGherkin, so we own a minimal, fully specified grammar covering the
subset the five-stage workflow needs — Feature, Background, Scenario,
Scenario Outline, Given/When/Then/And/But steps, and tags.

Not a general BDD engine: unknown keywords are a parse ERROR (fail fast,
P0' — a spec that only parses "mostly" is not machine-checkable).
"""

from __future__ import annotations

from dataclasses import dataclass, field

STEP_KEYWORDS = ("Given", "When", "Then", "And", "But")
BLOCK_KEYWORDS = ("Feature:", "Background:", "Scenario:", "Scenario Outline:")


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
class Scenario:
    name: str
    line: int
    tags: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)


@dataclass
class Feature:
    name: str
    line: int
    tags: list[str] = field(default_factory=list)
    background: list[Step] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)


def _strip_comment(line: str) -> str:
    # '#' starts a comment only at the start of a token, per Gherkin convention.
    if line.lstrip().startswith("#"):
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
    return Scenario(name=stripped[len(keyword):].strip(), line=line_no, tags=tags)


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


def _apply_block_line(
    feature: Feature,
    stripped: str,
    current_steps: list[Step] | None,
    pending_tags: list[str],
    last_kind: str | None,
    path: str,
    line_no: int,
) -> tuple[list[Step], list[str], str | None]:
    if stripped.startswith("Background:"):
        return feature.background, pending_tags, last_kind
    scenario = _parse_scenario_header(stripped, pending_tags, line_no)
    if scenario is not None:
        feature.scenarios.append(scenario)
        return scenario.steps, [], last_kind
    first_word = stripped.split(None, 1)[0]
    if first_word in STEP_KEYWORDS:
        new_kind = _append_step(stripped, first_word, current_steps, last_kind, path, line_no)
        return current_steps, pending_tags, new_kind
    raise GherkinSyntaxError(path, line_no, f"unrecognized line — {stripped!r}")


def parse(text: str, path: str = "<feature>") -> Feature:
    """Parse .feature text into a Feature tree. Raises GherkinSyntaxError."""
    feature: Feature | None = None
    current_steps: list[Step] | None = None  # background or scenario steps
    pending_tags: list[str] = []
    last_kind: str | None = None  # for And/But chaining

    for line_no, raw in enumerate(text.splitlines(), start=1):
        stripped = _strip_comment(raw).strip()
        if not stripped:
            continue

        if stripped.startswith("@"):
            pending_tags.extend(_parse_tags(stripped, path, line_no))
            continue

        if stripped.startswith("Feature:"):
            if feature is not None:
                raise GherkinSyntaxError(path, line_no, "duplicate Feature:")
            feature = Feature(
                name=stripped[len("Feature:"):].strip(),
                line=line_no,
                tags=pending_tags,
            )
            pending_tags = []
            continue

        if feature is None:
            raise GherkinSyntaxError(path, line_no, f"line before Feature: — {stripped!r}")

        current_steps, pending_tags, last_kind = _apply_block_line(
            feature, stripped, current_steps, pending_tags, last_kind, path, line_no
        )

    if feature is None:
        raise GherkinSyntaxError(path, 1, "no Feature: line found")
    return feature
