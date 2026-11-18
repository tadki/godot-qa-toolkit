#!/usr/bin/env python3
"""SEE-1594 §SPEC-012 形状红线扫描：指定域内无超限文件 / 超限函数 / 空壳
模块（facade）。

- **文件尺寸**：任一 `.py` 文件物理行数 > `--max-file-lines`（默认 800）
  → 违规。
- **函数尺寸**：任一 `def` / `async def`（含方法与嵌套函数，AST 口径）
  物理行数 > `--max-function-lines`（默认 50）→ 违规。
- **空壳模块**：非 `__init__.py` 的模块，若自身零 def/class 定义、顶层
  语句仅为 import / `__all__` / 对导入名的赋值（纯 re-export facade）
  → 违规（"公开接口只有 CLI"——不允许为兼容旧路径保留空壳）。

签名：
    python tests/integration/check_see1594_shape.py \
        --areas mutation,risk,coverage,measurement \
        --max-file-lines 800 --max-function-lines 50 --out <json>

违规 → 非零退出并逐条列出位置；JSON 记录 before/after 可对账尺寸账。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "godot_qa_toolkit"


def _module_uses(path: Path) -> dict:
    """AST 摘要：文件行数、函数（含嵌套）尺寸表、顶层语句形态。"""
    text = path.read_text(encoding="utf-8")
    n_lines = len(text.splitlines())
    tree = ast.parse(text)
    funcs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append({
                "name": node.name,
                "lines": (node.end_lineno or node.lineno) - node.lineno + 1,
                "lineno": node.lineno,
            })
    top_imports = 0
    top_assigns = 0
    reexport_assigns = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            top_imports += 1
        elif isinstance(node, ast.Assign):
            top_assigns += 1
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if targets <= {"__all__"} or _assigned_from_names(node):
                reexport_assigns += 1
    own_defs = sum(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for node in tree.body)
    return {
        "lines": n_lines,
        "funcs": funcs,
        "top_imports": top_imports,
        "top_assigns": top_assigns,
        "reexport_assigns": reexport_assigns,
        "own_defs": own_defs,
    }


def _assigned_from_names(node: ast.Assign) -> bool:
    """赋值右侧是否全部为 Name（典型 re-export：`foo = _impl.foo`）。"""
    return isinstance(node.value, ast.Name)


def _is_facade(path: Path, info: dict) -> bool:
    """非 __init__.py 且零自身 def/class、顶层只有 import + 重导出赋值。"""
    if path.name == "__init__.py":
        return False
    return info["own_defs"] == 0 and info["top_imports"] > 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--areas", default="mutation,risk,coverage,measurement",
                    help="逗号分隔的 src/godot_qa_toolkit 子域")
    ap.add_argument("--max-file-lines", type=int, default=800)
    ap.add_argument("--max-function-lines", type=int, default=50)
    ap.add_argument("--out", default=None, help="JSON 结果落盘路径")
    args = ap.parse_args(argv)

    areas = [a.strip() for a in args.areas.split(",") if a.strip()]
    violations: list[dict] = []
    per_area: dict[str, dict] = {}

    for area in areas:
        root = SRC_ROOT / area
        if not root.is_dir():
            print(f"check_see1594_shape: unknown area {area!r}", file=sys.stderr)
            return 2
        files = sorted(root.rglob("*.py"))
        area_max_file = 0
        area_funcs_over = []
        for path in files:
            info = _module_uses(path)
            rel = path.relative_to(REPO_ROOT).as_posix()
            area_max_file = max(area_max_file, info["lines"])
            if info["lines"] > args.max_file_lines:
                violations.append({
                    "kind": "file_lines", "path": rel,
                    "lines": info["lines"], "limit": args.max_file_lines})
            for f in info["funcs"]:
                if f["lines"] > args.max_function_lines:
                    violations.append({
                        "kind": "function_lines", "path": rel,
                        "name": f["name"], "lines": f["lines"],
                        "lineno": f["lineno"],
                        "limit": args.max_function_lines})
                    area_funcs_over.append(f["name"])
            if _is_facade(path, info):
                violations.append({
                    "kind": "facade_module", "path": rel,
                    "top_imports": info["top_imports"],
                    "reexport_assigns": info["reexport_assigns"]})
        per_area[area] = {
            "files": len(files),
            "max_file_lines": area_max_file,
            "functions_over_limit": len(area_funcs_over),
        }

    result = {
        "tool": "check_see1594_shape",
        "ok": not violations,
        "areas": areas,
        "max_file_lines": args.max_file_lines,
        "max_function_lines": args.max_function_lines,
        "per_area": per_area,
        "violations": violations,
    }
    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    for v in violations:
        label = v.get("name") or ""
        print(f"VIOLATION {v['kind']}: {v['path']} {label} "
              f"lines={v['lines']} > {v['limit']}")
    print(f"check_see1594_shape: "
          f"{'OK' if not violations else str(len(violations)) + ' violations'}"
          f" (areas={','.join(areas)})")
    return 0 if not violations else 1


if __name__ == "__main__":
    sys.exit(main())
