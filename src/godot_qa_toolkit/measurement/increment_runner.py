"""方向①：增量重跑的插桩准备与脚本级执行（被 measurement/increment.py 消费）。

SEE-1587：attr 模板构建单源——fresh 路径复用 orchestrator._instrument_pair，
不再保留独立构建器（SEE-1585 原型教训：两条构建代码在 algo 键不变时会让
全量/增量产出不同归属形态的模板静默共存）；prepcache 命中路径必须连带
block_map 读回，否则行事实退化为块首行。
"""

from __future__ import annotations
from ..digest import sha256_text

import hashlib
import os
import tempfile
from pathlib import Path

from . import prepcache
from .instrument import _instrument_pair
from .orchestrator import _run_one_gut
from .probe_templates import (
    _ATTR_CTX_GD,
    _ATTR_CTX_RELPATH,
    _ATTR_FLUSHER_GD,
    _ATTR_PRE_HOOK_GD,
    _ATTR_RUN_UUID_PLACEHOLDER,
    _FLUSHER_RELPATH,
    _PRE_HOOK_RELPATH,
)


def _algo_for(causal: bool) -> str:
    from .instrument import _algo_used
    return _algo_used(prepcache.PROBE_ALGO_VERSION, True, causal)


def _attr_template_entry(root: Path, rel: str, src: str, src_sha: str,
                         recipe: dict, algo: str, causal: bool) -> tuple:
    """单文件 attr 模板获取：prep 缓存优先；未命中现建（单源
    _instrument_pair，归属粒度由 algo 键编码）。缓存命中必须连带 block_map
    读回——行事实按块展开，丢 block_map 会让行集退化为块首行（静默失真）。"""
    from ..registry.functions import build_file_registry

    entry = prepcache.load_entry(
        root, rel=rel, src_sha256=src_sha,
        recipe_hash=recipe["recipe_hash"], algo=algo)
    if entry is not None:
        return (entry["instrumented_template"],
                list(entry.get("probe_lines", [])),
                dict(entry.get("block_map") or {}), entry["registry"])
    registry = build_file_registry(src, file_label=rel)
    built = _instrument_pair(rel, src, registry, block_compress=True,
                             test_attribution=True, attribution_causal=causal)
    if "parse_error" in built:
        raise ValueError(
            f"increment attr template failed to parse for "
            f"{rel}: {built['parse_error']}")
    template = built["template_attr"]
    if template is None:
        raise ValueError(f"increment attr template missing for {rel}")
    return template, built["probe_lines"], built["block_map"], registry


def prepare_attr_templates(root: Path, target_files: list[str],
                           recipe: dict, causal: bool) -> dict:
    """每目标文件准备 attr 模板（prep 缓存优先，未命中现建）；返回
    {rel: {abs, src, template(占位符形态), probe_lines, block_map, key8,
    registry}}。模板构建单源：fresh 路径与全量 run_measurement 共用
    orchestrator._instrument_pair（SEE-1585 教训）。"""
    algo = _algo_for(causal)
    out: dict[str, dict] = {}
    for path_str in target_files:
        p = Path(path_str)
        if not p.is_file():
            continue
        src = p.read_text(encoding="utf-8")
        rel = str(p.resolve().relative_to(root))
        key8 = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:8]
        src_sha = sha256_text(src)
        template, probe_lines, block_map, registry = _attr_template_entry(
            root, rel, src, src_sha, recipe, algo, causal)
        out[rel] = {
            "abs": str(p.resolve()), "src": src, "template": template,
            "probe_lines": probe_lines, "block_map": block_map,
            "key8": key8, "registry": registry,
        }
    return out

def _rerun_stage(root: Path, templates: dict, hook_files: list,
                 run_uuid: str) -> tuple[dict, dict]:
    """插桩树 + 归属钩子写入（归属模板必须配 ATTR finalizer/pre-hook——
    默认 flusher 不排空 gqt_cov_attr：错配时 hits 正常、归属静默全空）。"""
    started_files: dict[str, str] = {}
    instrumented: dict[str, str] = {}
    for rel, item in templates.items():
        inst = item["template"].replace(prepcache.SINK_UUID_PLACEHOLDER,
                                        run_uuid)
        started_files[item["abs"]] = item["src"]
        instrumented[item["abs"]] = inst
    return started_files, instrumented


def _rerun_one(root: Path, name: str, args_head: list[str], timeout_s: int,
               xdg: str) -> dict:
    """单脚本 `-gselect` 跑 GUT（独立 XDG user://）；单脚本失败不断链。"""
    env = dict(os.environ)
    env["XDG_DATA_HOME"] = xdg
    argv_gut = [*args_head, f"-gselect={name}",
                "-gpre_run_script=res://" + _PRE_HOOK_RELPATH]
    try:
        r, _w = _run_one_gut(root, argv_gut, timeout_s,
                             env=env, gate_root=root)
        return {"script": name, "rc": r.returncode,
                "out": ((r.stdout or "") + "\n" + (r.stderr or ""))[-4000:],
                "error": None}
    except Exception as e:  # noqa: BLE001 — 单脚本失败不断链
        return {"script": name, "rc": None, "out": "", "error": str(e)}


def execute_rerun_scripts(root: Path, gut_dirs: list[str],
                          script_names: list[str], timeout_s: int,
                          templates: dict, causal: bool,
                          run_uuid: str) -> tuple[list[dict], list[str]]:
    """写入插桩树与钩子，逐脚本 `-gselect` 跑 GUT，finally 恢复/清钩子。
    返回 (逐脚本结果, xdg_homes)——xdg 由调用方读回 sink 后清理。"""
    started_files, instrumented = _rerun_stage(root, templates, None,
                                               run_uuid)
    # 归属模板必须配 ATTR finalizer/pre-hook（默认 flusher 不排空
    # gqt_cov_attr——实测踩过：错配默认 flusher 时 hits 正常、归属静默全空）
    flusher_gd = _ATTR_FLUSHER_GD
    pre_hook_gd = _ATTR_PRE_HOOK_GD.replace(_ATTR_RUN_UUID_PLACEHOLDER,
                                            run_uuid)
    extra = [(_ATTR_CTX_RELPATH, _ATTR_CTX_GD)] if causal else []
    hook_files = [(_FLUSHER_RELPATH, flusher_gd),
                  (_PRE_HOOK_RELPATH, pre_hook_gd)] + extra
    results: list[dict] = []
    xdg_homes: list[str] = []
    try:
        for abs_path, inst in instrumented.items():
            Path(abs_path).write_text(inst, encoding="utf-8")
        for relpath, text in hook_files:
            abs_p = root / relpath
            abs_p.parent.mkdir(parents=True, exist_ok=True)
            abs_p.write_text(text, encoding="utf-8")
        args_head = ([("-gdir=" + ",".join(gut_dirs))] if gut_dirs else [])
        args_head += ["-ginclude_subdirs"]
        for name in script_names:
            xdg = tempfile.mkdtemp(prefix=f"gqt-incr-{run_uuid[:8]}-")
            xdg_homes.append(xdg)
            results.append(_rerun_one(root, name, args_head, timeout_s, xdg))
    finally:
        for abs_path, original in started_files.items():
            Path(abs_path).write_text(original, encoding="utf-8")
        for relpath, _text in hook_files:
            Path(root / relpath).unlink(missing_ok=True)
    return results, xdg_homes