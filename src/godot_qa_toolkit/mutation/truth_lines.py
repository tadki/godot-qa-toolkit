"""SEE-1594 BTIDY（零行为拆分）：truth-select 行级/物化/运行时归属支撑——
行规格解析、行→脚本集、脚本路径解析、选择目录物化与 RuntimeOwnership。
自 truth_select.py 原行段物理迁移；truth_select 侧以导入绑定保持既有
引用面（CLI 才是公开接口，内部路径迁移不构成契约变更）。"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from .truth_select import SELECT_ROOT  # noqa: F401 — 引用面保持


def _ts():
    """运行时经 truth_select 模块属性解析（避免循环导入）。"""
    import godot_qa_toolkit.mutation.truth_select as _m

    return _m


def parse_lines_spec(spec: str) -> list[int]:
    """`--lines` 规格解析：单行 / 连续区间 / 多段区间（逗号分隔）。

    `"42"` / `"10-20"` / `"10-20,30,35-40"` → 去重升序行号列表。
    非法形态（非整数 / 倒序区间 / 非正行号 / 空规格）→ ValueError
    （CLI 按参数错误契约转 ok=false，不与"无位点"混淆）。"""
    lines: set[int] = set()
    for seg in str(spec).split(","):
        seg = seg.strip()
        if not seg:
            raise ValueError(f"invalid --lines segment (empty): {spec!r}")
        if "-" in seg:
            lo_s, _, hi_s = seg.partition("-")
            lo, hi = int(lo_s), int(hi_s)
            if lo <= 0 or hi < lo:
                raise ValueError(
                    f"invalid --lines range {seg!r} (need 1<=lo<=hi)")
            lines.update(range(lo, hi + 1))
        else:
            n = int(seg)
            if n <= 0:
                raise ValueError(f"invalid --lines value {seg!r} (need >=1)")
            lines.add(n)
    if not lines:
        raise ValueError(f"empty --lines spec: {spec!r}")
    return sorted(lines)

def _line_script_sets(doc: dict, rel: str,
                      lines: set[int]) -> dict[int, set[str]]:
    """行 → 触达它的测试脚本 basename 集合。

    test 车道（line_to_tests）∪ script 车道（script_to_lines，before_all
    等脚本级执行同样是该脚本的测试面）。行不在任何行事实 = 空集 =
    no_mapped_tests。身份两侧统一 basename（注册表身份是全路径）。"""
    arm = (doc.get("arms") or {}).get("gut_full") or {}
    out: dict[int, set[str]] = {}
    for ln_s, idents in (arm.get("line_to_tests") or {}).get(rel,
                                                            {}).items():
        ln = int(ln_s)
        if ln not in lines:
            continue
        out.setdefault(ln, set()).update(
            Path(i.split("::", 1)[0]).name for i in idents)
    for script, rel_map in (arm.get("script_to_lines") or {}).items():
        for ln in (rel_map or {}).get(rel, []) or []:
            if int(ln) in lines:
                out.setdefault(int(ln), set()).add(Path(script).name)
    return out

def _resolve_script_paths(by_name: dict,
                          names) -> "tuple[list[str], list[str]]":
    """basename → tests 树路径解析（缺失名跳过、多路径记歧义）。

    返回 (scripts, ambiguous)，歧义由调用方 fail-loud。行级选面的两个
    消费点（resolve_line_group_selection / executor._line_group_chunks）
    单源共用；scripts 顺序为 sorted(names)，materialize_selection 内部
    再排序，物化产物与传入顺序无关。"""
    scripts: list[str] = []
    ambiguous: list[str] = []
    for name in sorted(names):
        paths = by_name.get(name)
        if not paths:
            continue
        if len(paths) > 1:
            ambiguous.append(name)
        scripts.extend(paths)
    return scripts, ambiguous

def _tests_for_target(root: str, arm: dict, rel: str) -> tuple[list[str], list[str]]:
    names: set[str] = set()
    for identity, files in (arm.get("test_to_files") or {}).items():
        if rel in files:
            names.add(identity.split("::", 1)[0])
    for identity, files in (arm.get("script_to_files") or {}).items():
        if rel in files:
            names.add(identity)
    by_name = _ts()._test_scripts_by_name(root)
    scripts: list[str] = []
    ambiguous: list[str] = []
    for name in sorted(names):
        paths = by_name.get(name)
        if not paths:
            continue
        if len(paths) > 1:
            ambiguous.append(name)
        scripts.extend(paths)
    return sorted(set(scripts)), ambiguous

def materialize_selection(project_root: str, scripts: list[str]) -> str:
    """选择目录：每个脚本一个同名 extends 包装（GUT 继承收集，身份=原名）。

    目录按脚本集内容哈希命名，已存在即复用（幂等）；返回 res:// 目录。"""
    sel_id = hashlib.sha1(
        "\n".join(sorted(scripts)).encode("utf-8")).hexdigest()[:12]
    root = Path(project_root)
    sel_fs = root / SELECT_ROOT / sel_id
    if not sel_fs.is_dir():
        sel_fs.mkdir(parents=True, exist_ok=True)
        for res_path in sorted(scripts):
            if not res_path.startswith("res://"):
                continue
            name = res_path.rsplit("/", 1)[-1]
            wrapper = sel_fs / name
            if wrapper.exists():
                continue
            wrapper.write_text(
                f'extends "{res_path}"\n', encoding="utf-8")
    return f"res://{SELECT_ROOT}/{sel_id}/"

class RuntimeOwnership:
    """kill 证书归属：测试名 → 脚本（来自新鲜映射身份；歧义/缺失 → None）。

    无映射或过期时由调用方传 None —— 证书不产出（fail-safe 只偏向重跑），
    不与静态归属索引混用（A09 后静态索引已退役）。"""

    __test__ = False

    def __init__(self, project_root: str, doc: dict):
        self.project_root = str(Path(project_root).resolve())
        self._by_name: dict[str, set[str]] = {}
        arm = doc["arms"]["gut_full"]
        for identity in (arm.get("test_to_files") or {}):
            script, _, name = identity.partition("::")
            self._by_name.setdefault(name, set()).add(script)
        for identity in (arm.get("script_to_files") or {}):
            if "::" in identity:
                script, _, name = identity.partition("::")
                self._by_name.setdefault(name, set()).add(script)
        self._name_to_path = _ts()._test_scripts_by_name(self.project_root)

    def owner_of(self, test_name: str) -> str | None:
        owners = self._by_name.get(test_name)
        if not owners or len(owners) != 1:
            return None
        script = next(iter(owners))
        paths = self._name_to_path.get(script, ())
        if len(paths) != 1:
            return None
        return paths[0]

def ownership_for(project_root: str, target_file: str,
                  map_path_override: str | None = None):
    """新鲜映射 → RuntimeOwnership；无新鲜映射/解析异常 → None（A09）。

    None = 不产出 kill 证书 → killed 保守重跑（fail-safe 只偏向重跑）。
    解析异常具名披露到 stderr（不吞错）；绝不回退静态归属索引
    （结构性兜底已退役——静态归属会给 stale 索引签发 kill 证书）。"""
    import sys
    try:
        sel = _ts().resolve_selection(project_root, target_file, map_path_override)
        mp = sel.get("mapping") or {}
        if mp.get("file_sha256_bound") and mp.get("recipe_hash_ok"):
            doc = _ts().load_map(mp["path"])
            owner = RuntimeOwnership(project_root, doc)
            owner.source = "runtime_mapping"
            return owner
        print(f"ownership: no fresh runtime mapping for {target_file} "
              "(no kill certificate — conservative rerun)", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001 — 具名披露后按无新鲜映射处理
        print(f"ownership: resolution failed ({type(e).__name__}: {e}) — "
              "no kill certificate (conservative rerun)", file=sys.stderr)
        return None
