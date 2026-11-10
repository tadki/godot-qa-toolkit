"""Source-file reading helper shared by parity/risk/coverage (§SPEC-028).

Read failures (missing file, non-UTF-8 bytes, OS errors) are isolated per
file and reported structurally instead of crashing the scan: parity routes
them to cc_unmeasured, risk routes them to the read_error gap lane, and
coverage returns a structured ok=false result. The `unreadable` reason
wording aligns with complexity/collector.py's existing contract.
"""

from __future__ import annotations

from pathlib import Path


def read_gd_source(path: str | Path) -> tuple[str | None, str | None]:
    """Read a GDScript source file. Returns (text, None) on success and
    (None, reason) on any read failure — the caller decides the lane; this
    helper never raises."""
    try:
        return Path(path).read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return None, f"unreadable: {type(e).__name__}: {e}"
