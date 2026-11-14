"""SEE-1321 SPEC-001：测试子集自动扫描器。

目标 .gd → 扫描 tests/ 树下引用它的测试文件（preload(path)/load(path) 字面量、
class_name 标识符引用两种形态），映射为最小命中目录的 `res://tests/<dir>/`
GUT glob。扫描零命中返回 None——由 runner 自动退回全量。
"""

from __future__ import annotations

import re
import sys
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


# E2（§SPEC-501）：零引用短路披露面——stderr 一行 + summary 定终态记录。
# 判据唯一出处 = derive_tests_glob 零命中（派生 glob，禁止 CLI 参数推断——
# SEE-1568 批次二 v1 串行门误判教训）；串行/并行路径共用同一判据与记录形态。
_ZERO_REF_NOTE = ("zero-ref short-circuit: no test references the target "
                  "— full-GUT fallback skipped (disclosed short-circuit; "
                  "potential kills intentionally not executed)")


def disclose_zero_ref(file_path: str) -> None:
    """零引用短路 stderr 披露行（与 summary.zero_ref_short_circuit 双披露）。"""
    print(f"zero-ref short-circuit: {file_path} — no test references the "
          f"target, full-GUT fallback skipped (disclosed)", file=sys.stderr)


def zero_ref_short_circuit_result(file_path: str) -> dict:
    """零引用目标定终态：有意不执行 + 双披露（形态与 no_sites 契约同构）。"""
    return {
        "tool": "mutation",
        "ok": True,
        "summary": {
            "file": file_path, "mutants": 0, "jobs": 0,
            "killed": 0, "survived": 0, "timeout": 0,
            "run_errors": 0, "suspect": 0, "invalid_mutants": 0,
            "kill_rate": 0.0,
            "note": _ZERO_REF_NOTE,
            "scoped": False,
            "zero_ref_short_circuit": True,
        },
        "failures": [],
    }


def derive_tests_glob(target_res_path: str, tests_dir: Path,
                      project_root: str | None = None) -> str | None:
    """目标 → 最小命中测试目录的 res:// glob（SPEC-001 主判据）。

    §SPEC-613（SEE-1576 R-7）：选测判据 = **直连引用强度**——仅 depth==1
    （静态四源直接命中）consumer 计权，权重 = 文件内目标身份 token 命中
    次数；传递闭包成员保留候选但零权重（闭包证据是对中间节点的证据，
    冒充目标证据会让弱耦合大目录以文件计数压过专用套件）。纯闭包（无
    任何直接引用）回退目录文件计数并标记 weak（保持可跑、不短路）。
    完整披露元数据经 derive_tests_glob_meta 返回。
    """
    glob, _meta = derive_tests_glob_meta(target_res_path, tests_dir,
                                         project_root=project_root)
    return glob


def derive_tests_glob_meta(target_res_path: str, tests_dir: Path,
                           project_root: str | None = None
                           ) -> tuple[str | None, dict]:
    """derive_tests_glob + 选测披露元数据（summary.test_selection 的唯一出处）。

    meta 键：derived_glob / strength_score / direct_files / weak
    （weak=True 时另有 fallback 与 suggestion）。零消费者 → glob None
    （E2 零引用短路判据不变），meta 同样给出 weak=True 形态。
    """
    if project_root is not None:
        glob, meta = _strength_glob(project_root, target_res_path, tests_dir)
    else:
        # SEE-1400 反查面（project_root=None）：scan_test_ref_counts 本就是
        # 引用次数强度语义，与直连强度同构——glob 结果零回归。
        counts = scan_test_ref_counts(target_res_path, tests_dir)
        glob = _glob_from_counts(counts, tests_dir) if counts else None
        meta = {"derived_glob": glob,
                "strength_score": sum(counts.values()),
                "direct_files": len(counts),
                "weak": not counts}
    return glob, meta


def selection_disclosure(selection_meta: dict | None) -> dict:
    """§SPEC-613 选测披露片段（summary 透出的唯一拼装点）。

    自动选测且派生 glob 在场 → {"test_selection": meta}；显式 --tests、
    零消费者或空 meta → 空片（update 即无操作）。四个披露位（串行门 /
    规划无分片 / _inject_one / runner disclosures）共用本口径。
    """
    if selection_meta and (selection_meta.get("derived_glob")
                           or selection_meta.get("source")):
        return {"test_selection": selection_meta}
    return {}


def _strength_glob(project_root: str, target_res_path: str,
                   tests_dir: Path) -> tuple[str | None, dict]:
    """gdself 闭包 → 直连强度选测（R-7 判据实现，含弱选测回退）。"""
    from .gdself import build_index, query_consumers
    cache_key = str(Path(project_root).resolve())
    index = _INDEX_CACHE.get(cache_key)
    if index is None:
        index = build_index(project_root)
        _INDEX_CACHE[cache_key] = index
    out = query_consumers(project_root, target_res_path, tests_dir,
                          index=index)
    tests_abs = str(tests_dir.absolute()).replace("\\", "/")
    direct: dict[str, int] = {}  # fs_path -> depth（仅 depth==1 进入）
    closure: dict[str, int] = {}  # fs_path -> len(sources)（回退排序用）
    for consumer in out["consumers"]:
        if not consumer["res"].startswith("res://tests/"):
            continue  # 生产/资产消费者只作闭包中转，不进 tests glob
        fs_path = str(Path(project_root).resolve()
                      / consumer["res"].removeprefix("res://"))
        closure[fs_path] = closure.get(fs_path, 0) + len(consumer["sources"])
        if consumer["depth"] == 1:
            direct[fs_path] = 1
    if not closure:
        return None, {"derived_glob": None, "strength_score": 0,
                      "direct_files": 0, "weak": True}
    if len(closure) == 1:
        # 唯一候选测试文件（无论直接/闭包）→ 文件级 glob（-gselect 通道），
        # 与旧单文件规则同形；判据不因直连证据缺失而收窄。
        only = next(iter(closure))
        rel = only[len(tests_abs):].strip("/")
        glob = "res://tests/" + rel
        score = _direct_strength_counts(project_root,
                                        target_res_path, direct).get(only, 0)
        return glob, {"derived_glob": glob, "strength_score": score,
                      "direct_files": 1 if score else 0,
                      "weak": not score}
    strength = _direct_strength_counts(project_root, target_res_path,
                                       direct)
    if not strength:
        # 纯闭包：无任何直接引用 → 回退既有目录文件计数（保持可跑，
        # 不误触 E2 短路），并标记弱选测供披露与 --tests 覆写建议。
        glob = _glob_from_counts(closure, tests_dir)
        return glob, {"derived_glob": glob, "strength_score": 0,
                      "direct_files": 0, "weak": True,
                      "fallback": "dir_file_count", "suggestion": ""}
    glob, score, files = _rank_by_strength(strength, tests_dir)
    return glob, {"derived_glob": glob, "strength_score": score,
                  "direct_files": files, "weak": False}


def _target_strength_words(project_root: str,
                           target_res_path: str) -> list[re.Pattern]:
    """目标身份 token（class_name/autoload/stem）+ 目标函数名 → 整词模式。

    函数名 token 是关键信号：专用套件按 API 语义调用（add_tag/has_tag/
    advance…），而 setup 清理型弱引用只出现身份名（TagManager._targets.clear）
    ——仅数身份 token 无法区分二者（F7 实证）。目标读失败不静默：降级为
    仅身份 token 并披露到 stderr。
    """
    from .gdself import _identities, _target_key_of
    index = _INDEX_CACHE[str(Path(project_root).resolve())]
    ident = _identities(index, _target_key_of(index, target_res_path))
    tokens = {str(ident[k]).casefold() for k in
              ("class_name", "autoload", "stem") if ident.get(k)}
    try:
        target_src = Path(project_root, target_res_path.removeprefix("res://")
                          ).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"test selection: unreadable target {target_res_path}: {e}",
              file=sys.stderr)
        target_src = ""
    tokens.update(m.casefold() for m in re.findall(
        r"^(?:static\s+)?func\s+([A-Za-z_]\w*)", target_src, re.M))
    return [re.compile(r"\b" + re.escape(t) + r"\b") for t in tokens]


def _direct_strength_counts(project_root: str, target_res_path: str,
                            direct: dict[str, int]) -> dict[str, int]:
    """直连 consumer 文件 → 目标引用强度（res 字面量 + token 整词命中次数）。

    consumer 读失败不静默：该文件按 0 权降级（不影响其他候选），披露到
    stderr。
    """
    words = _target_strength_words(project_root, target_res_path)
    counts: dict[str, int] = {}
    for fs_path in direct:
        try:
            text = Path(fs_path).read_text(encoding="utf-8",
                                           errors="replace")
        except OSError as e:
            print(f"test selection: unreadable consumer {fs_path}: {e}",
                  file=sys.stderr)
            continue
        text_cf = text.casefold()
        occ = sum(1 for r in _RES_REF_PATTERN.findall(text)
                  if _ref_hits_target(r, target_res_path))
        occ += sum(len(w.findall(text_cf)) for w in words)
        if occ:
            counts[fs_path] = occ
    return counts


def _rank_by_strength(counts: dict[str, int],
                      tests_dir: Path) -> tuple[str | None, int, int]:
    """目录分 = Σ 直连强度；全序 tie-break：Σ → 单文件峰值 → 直连文件数 → 深度 → 字典序。"""
    tests_abs = str(tests_dir.absolute()).replace("\\", "/")
    dir_score: dict[str, int] = {}
    dir_peak: dict[str, int] = {}
    dir_files: dict[str, int] = {}
    for path, n in counts.items():
        parent = path.rsplit("/", 1)[0]
        rel = parent[len(tests_abs):].strip("/")
        dir_score[rel] = dir_score.get(rel, 0) + n
        dir_peak[rel] = max(dir_peak.get(rel, 0), n)
        dir_files[rel] = dir_files.get(rel, 0) + 1
    ranked = sorted(dir_score,
                    key=lambda d: (-dir_score[d], -dir_peak[d],
                                   -dir_files[d], -d.count("/"), d))
    best = ranked[0]
    if not best:
        return "res://tests/", dir_score[best], dir_files[best]
    return ("res://tests/" + best + "/", dir_score[best], dir_files[best])


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


# E6-4（§SPEC-503）：gdself 索引进程内复用——规划相位只读、按 project_root
# 命中即复用、零持久化（进程级保守口径，跨 run 场景以新进程天然失效）。
_INDEX_CACHE: dict[str, dict] = {}
