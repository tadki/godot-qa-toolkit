"""Canonical full recipe for SEE-1546 WP3 (§SPEC-034).

Read-only builder: scans the project's `.gutconfig.json` GUT face and
`acceptance/features` set, folds in harness/toolchain versions, and hashes the
normalized recipe (canonical-recipe/1). Exclusions REQUIRE a reason — an empty
reason makes the recipe illegal. PR-A is the builder only: it never runs GUT,
never instruments, and never claims certified (single round is exploratory;
WP5 owns two-round certification).
"""

from __future__ import annotations
