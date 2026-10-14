"""SEE-1321 SPEC-001：测试子集自动扫描器。

目标 .gd → 扫描 tests/ 树下引用它的测试文件（preload(path)/load(path) 字面量、
class_name 标识符引用两种形态），映射为最小命中目录的 `res://tests/<dir>/`
GUT glob。扫描零命中返回 None——由 runner 自动退回全量。
"""

from __future__ import annotations

import re
from pathlib import Path

# res:// 引用字面量：preload("x") / load("x") 两函数形态（与 class_name
# 标识符引用合计 SPEC-001 三形态）
_RES_REF_PATTERN = re.compile(r'\b(?:preload|load)\s*\(\s*"([^"]+)"\s*\)')

# 判定“测试目录”的候选 glob 起点——扫描只在这棵树内做，误扫到 src/ 会命中自身
_TESTS_DIR_NAME = "tests"


def _res_canonical(res_path: str) -> str:
    return res_path.strip().casefold()


def scan_test_refs(target_res_path: str, tests_dir: Path) -> list[str]:
    """返回 tests/ 下引用目标文件的测试文件绝对路径列表（每文件一条，排序）。"""
    return sorted(scan_test_ref_counts(target_res_path, tests_dir))


def scan_test_ref_counts(target_res_path: str, tests_dir: Path) -> dict[str, int]:
    """每命中文件 → 命中引用次数（res:// 字面量 + class_name 标识符各计 1）。"""
    target_stem = Path(target_res_path).stem.casefold()
    # 全词匹配——子串计数会把 mock_save_manager.gd 内容误配进 save_manager
    # 的命中集（LOW-3 审查实证）
    stem_word = re.compile(r"\b" + re.escape(target_stem) + r"\b")
    counts: dict[str, int] = {}
    for gd in sorted(tests_dir.rglob("*.gd")):
        text = gd.read_text(encoding="utf-8", errors="replace")
        text_cf = text.casefold()
        score = sum(
            1 for r in _RES_REF_PATTERN.findall(text)
            if _ref_hits_target(r, target_res_path)
        ) + len(stem_word.findall(text_cf))
        if score:
            counts[str(gd.absolute()).replace("\\", "/")] = score
    return counts


def _ref_hits_target(ref: str, target_res_path: str) -> bool:
    ref_cf = ref.casefold().strip()
    target_cf = target_res_path.casefold().strip()
    # 精确或后缀匹配；“路径+尾巴 .bak” 这类串改别的文件的字面量不算
    return ref_cf == target_cf or ref_cf.endswith("/" + target_cf.rsplit("res://", 1)[-1])


def derive_tests_glob(target_res_path: str, tests_dir: Path,
                      project_root: str | None = None) -> str | None:
    """目标 → 最小命中测试目录的 res:// glob（SPEC-001 主判据）。

    SEE-1400 §SPEC-005：命中恰好 1 个测试文件 → 返回该文件 res:// 路径
    （.gd 结尾，走 _run_gut_on_project 的 -gselect 单文件通道）——单轮成本
    从目录级收敛到文件级。多命中维持目录级现状。映射保守方向不变：
    过窄只会假存活，不虚报质量。
    命中分布跨多个目录时取命中文件数第一个的最深目录（从浅到深按命中数
    与路径深度加权）——扫描误差方向保守：过窄只会少报 killed（生存假阳），
    不会虚报质量。

    SEE-1405 §SPEC-004 换源零耦合：project_root 显式传入时内部换源为
    gdself 正向索引（间接消费闭包可见）；None 时维持 SEE-1400 反查语义
    （既有调用方/判据零回归）。tie-break 两源共用同一实现，不漂移。
    project_root 必须显式传，禁止 tests_dir.parent 隐推（定案条款）。
    """
    if project_root is not None:
        counts = _index_ref_counts(project_root, target_res_path, tests_dir)
    else:
        counts = scan_test_ref_counts(target_res_path, tests_dir)
    if not counts:
        return None
    return _glob_from_counts(counts, tests_dir)


def _glob_from_counts(counts: dict[str, int], tests_dir: Path) -> str | None:
    """命中文件计数 → glob；文件级/目录级/tie-break 语义唯一出处。"""
    tests_abs = str(tests_dir.absolute()).replace("\\", "/")
    if len(counts) == 1:
        only = next(iter(counts))
        rel = only[len(tests_abs):].strip("/")
        # 单文件在 tests/ 根直接命中（rel 无目录成分）→ res://tests/<name>.gd
        return "res://tests/" + rel
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


def _index_ref_counts(project_root: str, target_res_path: str,
                      tests_dir: Path) -> dict[str, int]:
    """gdself 正向索引 → 每命中测试文件计数（keys 与反查同形：绝对路径）。

    计数 = 该测试文件的命中源种类数（path/class/autoload/stem/dynamic），
    与反查的「引用次数」同为强度代理，供同一 tie-break 消费。
    """
    from .gdself import query_consumers
    out = query_consumers(project_root, target_res_path, tests_dir)
    tests_abs = str(tests_dir.absolute()).replace("\\", "/")
    counts: dict[str, int] = {}
    for consumer in out["consumers"]:
        if not consumer["res"].startswith("res://tests/"):
            continue  # 生产/资产消费者只作闭包中转，不进 tests glob
        fs_path = str(Path(project_root).resolve()
                      / consumer["res"].removeprefix("res://"))
        counts[fs_path] = counts.get(fs_path, 0) + len(consumer["sources"])
    return counts
