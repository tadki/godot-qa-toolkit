"""SEE-1321 SPEC-001：测试子集自动扫描器。

目标 .gd → 扫描 tests/ 树下引用它的测试文件（preload(path)/load(path) 字面量、
class_name 标识符引用两种形态），映射为最小命中目录的 `res://tests/<dir>/`
GUT glob。扫描零命中返回 None——由 runner 自动退回全量。
"""

from __future__ import annotations

import re
from pathlib import Path

# res:// 引用字面量：preload("x") / load("x") / load("x") 三函数形态
_RES_REF_PATTERN = re.compile(r'\b(?:preload|load)\s*\(\s*"([^"]+)"\s*\)')
# class_name 标识符引用：按 .gd 文件名 stem 全词匹配（SaveManager ↔ save_manager.gd）
_STEM_TOKEN_PATTERN = re.compile(r"\w+")

# 判定“测试目录”的候选 glob 起点——扫描只在这棵树内做，误扫到 src/ 会命中自身
_TESTS_DIR_NAME = "tests"


def _res_canonical(res_path: str) -> str:
    return res_path.strip().casefold()


def _target_identities(path: Path) -> set[str]:
    """目标文件的引用等价身份：res:// 绝对路径（canonical）+ stem。"""
    stem = path.stem.casefold()
    rel = str(path).replace("\\", "/")
    # 绝对路径尽力归一为 res://（相对 KOL 项目根的调用形态不断变化，
    # 接受 caller 传入的任何形式，casefold 后前后缀比对兜住）
    identities = {stem}
    if "res://" not in rel.casefold():
        idx = rel.casefold().find("/tests/")
        if idx >= 0:
            identities.add("res://" + rel[idx + 1:].casefold())
        identities.add(_res_canonical("res://" + path.name))
    else:
        identities.add(_res_canonical(rel))
    return identities


def scan_test_refs(target_res_path: str, tests_dir: Path) -> list[str]:
    """返回 tests/ 下引用目标文件的测试文件绝对路径列表（每文件一条，排序）。"""
    return sorted(scan_test_ref_counts(target_res_path, tests_dir))


def scan_test_ref_counts(target_res_path: str, tests_dir: Path) -> dict[str, int]:
    """每命中文件 → 命中引用次数（res:// 字面量 + class_name 标识符各计 1）。"""
    target_stem = Path(target_res_path).stem.casefold()
    counts: dict[str, int] = {}
    for gd in sorted(tests_dir.rglob("*.gd")):
        text = gd.read_text(encoding="utf-8", errors="replace")
        text_cf = text.casefold()
        score = sum(
            1 for r in _RES_REF_PATTERN.findall(text)
            if _ref_hits_target(r, target_res_path)
        ) + text_cf.count(target_stem)
        if score:
            counts[str(gd.absolute()).replace("\\", "/")] = score
    return counts


def _ref_hits_target(ref: str, target_res_path: str) -> bool:
    ref_cf = ref.casefold().strip()
    target_cf = target_res_path.casefold().strip()
    # 精确或后缀匹配；“路径+尾巴 .bak” 这类串改别的文件的字面量不算
    return ref_cf == target_cf or ref_cf.endswith("/" + target_cf.rsplit("res://", 1)[-1])


def _res_suffix_match(res_refs: set[str], target_res_path: str) -> bool:
    """res:// 引用按后缀匹配：preload 相对引用（autoload 场景）与绝对引用统一。"""
    canon = _res_canonical(target_res_path)
    return any(ref.endswith(canon) or canon.endswith(ref) for ref in res_refs)


def derive_tests_glob(target_res_path: str, tests_dir: Path) -> str | None:
    """目标 → 最小命中测试目录的 res:// glob（SPEC-001 主判据）。

    命中分布跨多个目录时取命中文件数第一个的最深目录（从浅到深按命中数
    与路径深度加权）——扫描误差方向保守：过窄只会少报 killed（生存假阳），
    不会虚报质量。
    """
    counts = scan_test_ref_counts(target_res_path, tests_dir)
    if not counts:
        return None
    tests_abs = str(tests_dir.absolute()).replace("\\", "/")
    dir_counts: dict[str, int] = {}
    for path, n in counts.items():
        parent = path.rsplit("/", 1)[0]
        rel = parent[len(tests_abs):].strip("/")
        # tests/ 根直接命中的文件（rel 为空）退回全量语义由调用方兜底
        dir_counts[rel] = dir_counts.get(rel, 0) + n
    # 引用数为主、路径更深 tie-break（子模块集成测试通常挂主目录下属）
    best = max(dir_counts.items(), key=lambda kv: (kv[1], kv[0].count("/")))
    if not best[0]:
        return "res://tests/"
    return "res://tests/" + best[0] + "/"
