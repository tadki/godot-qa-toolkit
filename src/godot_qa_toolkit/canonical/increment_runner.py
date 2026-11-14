"""方向①：增量重跑的插桩准备与脚本级执行（被 canonical/increment.py 消费）。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from . import prepcache
from .orchestrator import (
    _ATTR_CTX_GD,
    _ATTR_CTX_RELPATH,
    _ATTR_FLUSHER_GD,
    _ATTR_PRE_HOOK_GD,
    _ATTR_RUN_UUID_PLACEHOLDER,
    _FLUSHER_RELPATH,
    _PRE_HOOK_RELPATH,
    _function_entry_probes,
    _rewrite_call_sites,
    _run_one_gut,
)


def _algo_for(causal: bool) -> str:
    from .orchestrator import _algo_used
    return _algo_used(prepcache.PROBE_ALGO_VERSION, True, causal)


def _build_fresh(src: str, rel: str, key8: str, causal: bool,
                 registry: dict) -> tuple[str, list[int], dict]:
    """fresh 构建：先 plain 取块入口清单，再按函数入口 probe 建 attr 模板
    （与 run_canonical 同序，probe manifest 一致）。"""
    from ..coverage.runner import _instrument_blocks
    tpl_sink = (f"qa-coverage-hits-{prepcache.SINK_UUID_PLACEHOLDER}"
                f"-{key8}.txt")
    _, probe_lines, block_map = _instrument_blocks(
        src, tpl_sink, runtime_sink=True)
    inst_src = src
    if causal:
        inst_src, _ = _rewrite_call_sites(src, key8)
    ids, _fids = _function_entry_probes(registry, probe_lines)
    template, _pl2, _bm2 = _instrument_blocks(
        inst_src, tpl_sink, runtime_sink=True, attribution=True,
        causal=causal, attr_probe_ids=frozenset(ids))
    return template, probe_lines, block_map


def prepare_attr_templates(root: Path, target_files: list[str],
                           recipe: dict, causal: bool) -> dict:
    """每目标文件准备 attr 模板（prep 缓存优先，未命中现建）；返回
    {rel: {abs, src, template(占位符形态), probe_lines, attr_probe_ids,
    attr_fid_by_probe, key8, registry}}。"""
    from ..coverage.runner import _instrument_blocks
    from ..registry.functions import build_file_registry
    from gdtoolkit.parser import parser as gd_parser

    algo = _algo_for(causal)
    out: dict[str, dict] = {}
    for path_str in target_files:
        p = Path(path_str)
        if not p.is_file():
            continue
        src = p.read_text(encoding="utf-8")
        rel = str(p.resolve().relative_to(root))
        key8 = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:8]
        src_sha = hashlib.sha256(src.encode("utf-8")).hexdigest()
        entry = prepcache.load_entry(
            root, rel=rel, src_sha256=src_sha,
            recipe_hash=recipe["recipe_hash"], algo=algo)
        if entry is not None:
            template = entry["instrumented_template"]
            probe_lines = list(entry.get("probe_lines", []))
            registry = entry["registry"]
        else:
            registry = build_file_registry(src, file_label=rel)
            template, probe_lines, _bm = _build_fresh(src, rel, key8,
                                                      causal, registry)
            if probe_lines:
                try:
                    gd_parser.parse(template)
                except Exception as e:  # noqa: BLE001 — 插桩器 bug 拒跑
                    raise ValueError(
                        f"increment attr template failed to parse for "
                        f"{rel}: {e}") from e
        ids, fids = _function_entry_probes(registry, probe_lines)
        out[rel] = {
            "abs": str(p.resolve()), "src": src, "template": template,
            "probe_lines": probe_lines, "attr_probe_ids": frozenset(ids),
            "attr_fid_by_probe": fids, "key8": key8, "registry": registry,
        }
    return out


def execute_rerun_scripts(root: Path, gut_dirs: list[str],
                          script_names: list[str], timeout_s: int,
                          templates: dict, causal: bool,
                          run_uuid: str) -> tuple[list[dict], list[str]]:
    """写入插桩树与钩子，逐脚本 `-gselect` 跑 GUT，finally 恢复/清钩子。
    返回 (逐脚本结果, xdg_homes)——xdg 由调用方读回 sink 后清理。"""
    started_files: dict[str, str] = {}
    instrumented: dict[str, str] = {}
    for rel, item in templates.items():
        inst = item["template"].replace(prepcache.SINK_UUID_PLACEHOLDER,
                                        run_uuid)
        started_files[item["abs"]] = item["src"]
        instrumented[item["abs"]] = inst
    # 归属模板必须配 ATTR finalizer/pre-hook（默认 flusher 不排空
    # gqt_cov_attr——实测踩过：错配默认 flusher 时 hits 正常、归属静默全空）
    flusher_gd = _ATTR_FLUSHER_GD
    pre_hook_gd = _ATTR_PRE_HOOK_GD.replace(_ATTR_RUN_UUID_PLACEHOLDER,
                                            run_uuid)
    extra = [(_ATTR_CTX_RELPATH, _ATTR_CTX_GD)] if causal else []
    hook_files = [(_FLUSHER_RELPATH, flusher_gd),
                  (_PRE_HOOK_RELPATH, pre_hook_gd)] + extra
    xdg_homes: list[str] = []
    results: list[dict] = []
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
            env = dict(os.environ)
            env["XDG_DATA_HOME"] = xdg
            argv_gut = [*args_head, f"-gselect={name}",
                        "-gpre_run_script=res://" + _PRE_HOOK_RELPATH]
            try:
                r, _w = _run_one_gut(root, argv_gut, timeout_s,
                                     env=env, gate_root=root)
                rc = r.returncode
                out = (r.stdout or "") + "\n" + (r.stderr or "")
                err = None
            except Exception as e:  # noqa: BLE001 — 单脚本失败不断链
                rc, out, err = None, "", str(e)
            results.append({"script": name, "rc": rc, "out": out[-4000:],
                            "error": err})
    finally:
        for abs_path, original in started_files.items():
            Path(abs_path).write_text(original, encoding="utf-8")
        for relpath, _text in hook_files:
            Path(root / relpath).unlink(missing_ok=True)
    return results, xdg_homes
