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
    orchestrator._instrument_pair（SEE-1585 教训）。
    SEE-1610 §SPEC-N1：单文件插桩解析失败逐文件隔离（stderr 具名披露后
    跳过）——与全量路径 unmeasured 口径对称；绝不整体硬抛（一个不可插桩
    文件不得炸掉整个增量臂），也不静默（无披露的缩面 = 假新鲜）。"""
    import sys

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
        try:
            template, probe_lines, block_map, registry = _attr_template_entry(
                root, rel, src, src_sha, recipe, algo, causal)
        except Exception as e:  # noqa: BLE001 — 逐文件隔离，与全量
            # _prepare_fresh 的 BLE001 同宽：lark 原始异常不穿透增量臂
            print(f"measure: increment attr template skipped for {rel}: "
                  f"{type(e).__name__}: {e} — file excluded from this "
                  f"increment arm (unmeasured, same as full-face "
                  f"isolation)", file=sys.stderr)
            continue
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


def _scenario_map_from_manifests(out: dict, keys: "list[str]"
                                 ) -> "dict[str, str]":
    """编译 manifests → test_name → scenario 映射（零行为拆分）。"""
    mapping: dict[str, str] = {}
    for k in keys:
        for case in ((out["manifests"].get(k) or {}).get("cases") or []):
            tn = str(case.get("test_name") or "")
            sc = str(case.get("scenario") or "")
            if tn and sc:
                mapping[tn] = sc
    return mapping


def _gherkin_leg_compile_suite(root: Path, keys: list[str], run_uuid: str
                               ) -> "tuple[Path, dict, str, object, dict]":
    """编译 rerun keys 进私有 suite 并做 validator 预检恰好一次
    （§SPEC-003 契约：业务执行前同一 validator）。返回 (suite_dir,
    identities, gen_res, identity_rewrite, scenario_of_test)；编译/预检
    失败具名抛出。"""
    from ..gherkin.addon import AddonError, compile_preflight, \
        run_validator_preflight
    from ..gherkin.run import GherkinRunError, replay_check
    from ..gherkin.suite import SuiteError, compile_keys_to_suite
    from .attribution import _gherkin_identity_rewrite

    try:
        compile_preflight(root)
    except AddonError as e:
        raise GherkinRunError(f"gherkin preflight failed: {e}") from e
    suite_dir = root / "gherkin" / "generated" / f".gqt-run-{run_uuid[:12]}"
    if suite_dir.exists():
        raise SuiteError(
            f"private suite dir already exists: {suite_dir}")
    suite_dir.mkdir(parents=True)
    out = compile_keys_to_suite(root, keys, suite_dir)
    identities = {k: (out["manifests"].get(k) or {}).get("attribution")
                  or [] for k in keys}
    # §SPEC-025/D8（R4）：test_name → scenario 映射（执行账失败名回写
    # scenario 名用；manifest 在 suite 回收后仍可判定逐 scenario 红绿）。
    scenario_of_test = _scenario_map_from_manifests(out, keys)
    for k in keys:
        replay_check(root, k, suite_dir=suite_dir)
    try:
        run_validator_preflight(root)
    except AddonError as e:
        raise GherkinRunError(f"gherkin preflight failed: {e}") from e
    gen_res = ("res://" + suite_dir.joinpath("generated")
               .relative_to(root).as_posix())
    rw = _gherkin_identity_rewrite(root, [{"suite_dir": str(suite_dir),
                                           "keys": keys}], identities)
    return suite_dir, identities, gen_res, rw, scenario_of_test


def _gherkin_leg_texts(templates: dict, run_uuid: str,
                       causal: bool) -> "tuple[dict, dict, list]":
    """插桩文本与 ATTR 钩子清单：(started_files 原文, instrumented 插桩
    后文本, hook_files)。"""
    started_files: dict[str, str] = {}
    instrumented: dict[str, str] = {}
    for rel, item in templates.items():
        if "template" not in item:
            continue
        inst = item["template"].replace(prepcache.SINK_UUID_PLACEHOLDER,
                                        run_uuid)
        started_files[item["abs"]] = item["src"]
        instrumented[item["abs"]] = inst
    hook_files = [(_FLUSHER_RELPATH, _ATTR_FLUSHER_GD),
                  (_PRE_HOOK_RELPATH, _ATTR_PRE_HOOK_GD.replace(
                      _ATTR_RUN_UUID_PLACEHOLDER, run_uuid))]
    if causal:
        hook_files.append((_ATTR_CTX_RELPATH, _ATTR_CTX_GD))
    return started_files, instrumented, hook_files


def _gherkin_leg_execute(root: Path, gen_res: str, started_files: dict,
                         instrumented: dict, hook_files: list,
                         run_uuid: str, timeout_s: int,
                         xdg_homes: list[str]) -> list[dict]:
    """写插桩文本与 ATTR 钩子 → `-gdir` 单进程跑 suite（XDG 隔离）→
    finally 还原文树/清钩子。返回逐轮执行记录；xdg 目录只登记，由调用方
    成功路径回收（与既有口径一致，错误路径不新增回收）。"""
    import tempfile

    executions: list[dict] = []
    try:
        for abs_path, inst in instrumented.items():
            Path(abs_path).write_text(inst, encoding="utf-8")
        for relpath, text in hook_files:
            abs_p = root / relpath
            abs_p.parent.mkdir(parents=True, exist_ok=True)
            abs_p.write_text(text, encoding="utf-8")
        xdg = tempfile.mkdtemp(prefix=f"gqt-incr-gh-{run_uuid[:8]}-")
        xdg_homes.append(xdg)
        argv = [f"-gdir={gen_res}",
                "-gpre_run_script=res://" + _PRE_HOOK_RELPATH]
        env = dict(os.environ)
        env["XDG_DATA_HOME"] = xdg
        r, _w = _run_one_gut(root, argv, timeout_s, env=env,
                             gate_root=root)
        executions.append({"shard": gen_res, "rc": r.returncode,
                           "out": ((r.stdout or "") + "\n"
                                   + (r.stderr or ""))[-4000:],
                           "error": None})
    finally:
        for abs_path, original in started_files.items():
            Path(abs_path).write_text(original, encoding="utf-8")
        for relpath, _text in hook_files:
            Path(root / relpath).unlink(missing_ok=True)
    return executions


def run_gherkin_increment_leg(root: Path, feature_keys: list[str],
                              templates: dict, recipe: dict, causal: bool,
                              run_uuid: str, timeout_s: int
                              ) -> tuple[dict, list[dict]]:
    """SEE-1631 §SPEC-016：gherkin 臂增量重跑腿（只跑失效 feature）。

    编译 rerun keys 进私有 suite（捕获 manifest 身份）→ validator 预检
    恰好一次（§SPEC-003 契约：业务执行前同一 validator）→ 根树写插桩
    文本与 ATTR 钩子 → `-gdir` 单进程跑 suite（XDG 隔离）→ 归属读回
    （身份改写为 gherkin:// 规范形态）→ finally 还原/清钩子/回收 suite。
    返回 (fresh 臂级映射, 逐轮执行记录)。编译/预检失败具名抛出（SuiteError
    / GherkinRunError / AddonError），不得静默缩面。"""
    import shutil
    import sys

    from .arms import _cleanup_suite_dir
    from .attribution import (
        _fid_by_line_from_registry,
        build_attribution_map,
    )
    from .orchestrator import _read_attr_merged, _read_registries

    keys = sorted({str(k) for k in feature_keys})
    if not keys:
        return None, []
    suite_dir, _identities, gen_res, rw, scenario_of_test = \
        _gherkin_leg_compile_suite(root, keys, run_uuid)
    started_files, instrumented, hook_files = _gherkin_leg_texts(
        templates, run_uuid, causal)
    xdg_homes: list[str] = []
    try:
        executions = _gherkin_leg_execute(root, gen_res, started_files,
                                          instrumented, hook_files,
                                          run_uuid, timeout_s, xdg_homes)
        attr_by_rel = {}
        for rel, item in templates.items():
            attr_by_rel[rel] = _read_attr_merged(root, run_uuid, rel,
                                                 xdg_homes)
        registries = _read_registries(root, run_uuid, xdg_homes,
                                      consume=True)
        fid_by_line = {rel: _fid_by_line_from_registry(it["registry"])
                       for rel, it in templates.items()
                       if it.get("registry") is not None}
        fresh = build_attribution_map(
            "gherkin", attr_by_rel, registries,
            probe_lines_by_rel={rel: it["probe_lines"]
                                for rel, it in templates.items()},
            block_map_by_rel={rel: it["block_map"]
                              for rel, it in templates.items()},
            fid_by_line_by_rel=fid_by_line,
            identity_rewrite=rw)
        for xdg in xdg_homes:
            shutil.rmtree(xdg, ignore_errors=True)
        # §SPEC-025/D8（R4）：执行账失败名（编译 test_name）回写为
        # scenario 名（failing_scenarios）——verdict 重算按 scenario 粒度。
        if scenario_of_test:
            from .gutrun import _parse_failing_tests
            for ex in executions:
                failing = _parse_failing_tests(str(ex.get("out") or ""))
                ex["failing_scenarios"] = sorted(
                    {scenario_of_test[t] for t in failing
                     if t in scenario_of_test})
        return fresh, executions
    finally:
        try:
            _cleanup_suite_dir(suite_dir, root)
        except OSError as e:
            print(f"measure: increment gherkin suite cleanup failed: {e}",
                  file=sys.stderr)