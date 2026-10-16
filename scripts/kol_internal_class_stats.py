#!/usr/bin/env python3
"""§SPEC-1420-13：KOL（任意 Godot 项目）内部 class 统计脚本（定案 §四.8
验证物，判据可复现——Archi A1' 教训：统计口径必须脚本化钉死）。

口径（与 scopemanifest D1 同源，AST 判定不靠正则）：
- files_scanned：项目下全部 .gd（.qa-cache/.git/addons 排除）；
- files_with_inner_class：含 ≥1 个顶层 class_def 的文件数；
- inner_classes：顶层 class_def 总数（黑盒 scope 候选数）；
- methodless_classes：零方法的内部 class（D1 实证口径：KOL 内部 class 仅
  1 例 0 方法——嵌套粒度 YAGNI 的裁决依据，本期可复现复核）；
- funcs / module_site_bearing_scope_hint：scope 总量参考。

用法：
    python scripts/kol_internal_class_stats.py <project-root> [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gdtoolkit.parser import parser as gdtoolkit_parser

_EXCLUDED_DIRS = {".git", ".qa-cache", ".godot", "addons", "__pycache__"}
_FUNC_NODES = ("func_def", "static_func_def")
_CLASS_NODES = ("class_def",)


def _decl_name(node) -> str:
    for child in getattr(node, "children", []):
        if hasattr(child, "data"):
            found = _decl_name(child)
            if found:
                return found
        value = getattr(child, "value", "")
        if value and value[0].isalpha():
            return value
    return "anonymous"


def _methods_of_class(node) -> int:
    """class 子树内方法数（嵌套 class 的方法计入宿主——黑盒口径）。"""
    count = 0
    for child in getattr(node, "children", []):
        data = getattr(child, "data", None)
        if data in _FUNC_NODES:
            count += 1
        elif data is not None:
            count += _methods_of_class(child)
    return count


def stat_file(path: Path) -> dict:
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = gdtoolkit_parser.parse(src, gather_metadata=False)
    funcs = 0
    classes = []
    for child in tree.children:
        data = getattr(child, "data", None)
        if data in _FUNC_NODES:
            funcs += 1
        elif data in _CLASS_NODES:
            classes.append(child)
    return {
        "funcs": funcs,
        "classes": len(classes),
        "methodless": sum(1 for c in classes if _methods_of_class(c) == 0),
        "names": [_decl_name(c) for c in classes],
    }


def scan_project(root: Path) -> dict:
    agg = {"files_scanned": 0, "files_with_inner_class": 0,
           "inner_classes": 0, "methodless_classes": 0, "funcs": 0,
           "unparsable": 0}
    details = []
    for gd in sorted(root.rglob("*.gd")):
        if any(part in _EXCLUDED_DIRS for part in gd.relative_to(root).parts):
            continue
        agg["files_scanned"] += 1
        try:
            st = stat_file(gd)
        except Exception:
            agg["unparsable"] += 1
            continue
        agg["funcs"] += st["funcs"]
        agg["inner_classes"] += st["classes"]
        agg["methodless_classes"] += st["methodless"]
        if st["classes"]:
            agg["files_with_inner_class"] += 1
            details.append({"file": str(gd.relative_to(root)),
                            "classes": st["names"],
                            "methodless": st["methodless"]})
    agg["files_with_details"] = details
    return agg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", help="Godot project root to scan")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON (default: text)")
    args = ap.parse_args()
    root = Path(args.root)
    if not root.is_dir():
        print(f"project root not found: {root}", file=sys.stderr)
        return 2
    agg = scan_project(root)
    if args.json:
        print(json.dumps(agg, ensure_ascii=False, indent=2))
    else:
        details = agg.pop("files_with_details")
        print(f"files scanned          : {agg['files_scanned']}")
        print(f"unparsable (skipped)   : {agg['unparsable']}")
        print(f"files w/ inner class   : {agg['files_with_inner_class']}")
        print(f"inner classes (top)    : {agg['inner_classes']}")
        print(f"methodless inner class : {agg['methodless_classes']}")
        print(f"top-level funcs        : {agg['funcs']}")
        for d in details:
            print(f"  - {d['file']}: {d['classes']}"
                  f" (methodless: {d['methodless']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
