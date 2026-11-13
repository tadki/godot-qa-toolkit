"""Source path/reading helpers shared by parity/risk/coverage/mutation
(§SPEC-028, §SPEC-610).

Read failures (missing file, non-UTF-8 bytes, OS errors) are isolated per
file and reported structurally instead of crashing the scan: parity routes
them to cc_unmeasured, risk routes them to the read_error gap lane, and
coverage returns a structured ok=false result. The `unreadable` reason
wording aligns with complexity/collector.py's existing contract.
"""

from __future__ import annotations

from pathlib import Path


def anchor_to_project_root(file_path: str, project_root: str) -> str:
    """§SPEC-610（SEE-1576 R-5）：相对目标锚定 `--project-root`，绝对路径
    原样直过——目标解析不依赖调用方 CWD。

    mutation 多文件入口 / `run_mutation` / coverage `run_coverage` 三处
    同形态检查点共用本口径。找不到目标时的错误消息由调用方附加锚定路径。
    """
    if Path(file_path).is_absolute():
        return file_path
    return str(Path(project_root) / file_path)


def anchor_targets_to_project_root(files: list[str],
                                   project_root: str) -> list[str]:
    """`anchor_to_project_root` 的列表形态（mutation 多文件入口单点归一）。"""
    return [anchor_to_project_root(f, project_root) for f in files]


def read_gd_source(path: str | Path) -> tuple[str | None, str | None]:
    """Read a GDScript source file. Returns (text, None) on success and
    (None, reason) on any read failure — the caller decides the lane; this
    helper never raises."""
    try:
        return Path(path).read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return None, f"unreadable: {type(e).__name__}: {e}"
