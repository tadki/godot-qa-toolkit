"""SEE-1594 BTIDY（零行为拆分）：测量插桩工具链——algo 键、模板选择、
toolchain 注入、静态准备缓存、两臂插桩模板生成与单目标文件准备。自
orchestrator.py 原行段物理迁移；`_instrument_pair` 与 `_prepare_target_file`
按阶段分解为 ≤50 行函数，逻辑逐语句等价。"""

from __future__ import annotations

from pathlib import Path

from ..coverage.runner import (
    _instrument,
    _instrument_blocks,
    _rewrite_call_sites,
)
from ..digest import sha256_text
from ..mutation.inject import godot_command
from .attribution import ATTRIBUTION_ALGO_TAG
from .backup import file_key8
from .probe_templates import (
    _ATTR_FLUSHER_GD,
    _ATTR_PRE_HOOK_GD,
    _ATTR_RUN_UUID_PLACEHOLDER,
    _FLUSHER_GD,
    _PRE_HOOK_GD,
)


def _algo_used(base_algo: str, test_attribution: bool,
               causal: bool = False) -> str:
    """§SPEC-083：归属形态插桩产物不同 → 缓存键必须分形；默认关闭路径
    algo 原样返回（既有缓存条目继续命中，零漂移）。SEE-1587：归属粒度
    编码进 algo 键（attr-line/1 = hit 级探针面）——fn 级旧条目（attr/6）
    与 hit 级新条目键不同，绝不互相命中；R2 的创建点包装再分一层
    （同一归属开关下模板不同，不得互相复用）。"""
    if not test_attribution:
        return base_algo
    suffix = f"{ATTRIBUTION_ALGO_TAG}-causal" if causal else ATTRIBUTION_ALGO_TAG
    return f"{base_algo}-{suffix}"


def _hook_templates(test_attribution: bool,
                    run_uuid: str) -> tuple[str, str]:
    """§SPEC-083：归属形态换用 per-test 钩子与 attr finalizer；默认路径
    返回既有模板（字节不变）。"""
    if test_attribution:
        return (_ATTR_FLUSHER_GD,
                _ATTR_PRE_HOOK_GD.replace(
                    _ATTR_RUN_UUID_PLACEHOLDER, str(run_uuid)))
    return _FLUSHER_GD, _PRE_HOOK_GD


def _inject_toolchain(root: Path, recipe: dict) -> dict:
    """PR-A L1 预留的编排器职责：测量时注入真实 toolchain 版本并重算
    recipe_hash（注入路径下版本变化即 recipe 变化）。加载态先 verify
    （防篡改），注入后重 hash 再作为测量身份。"""
    import re as _re

    from ..gherkin import OPS_VERSION
    toolchain = dict(recipe.get("toolchain", {}))
    toolchain["ops_version"] = OPS_VERSION
    try:
        from importlib.metadata import version as _pkg_version
        toolchain["gqt_version"] = _pkg_version("godot-qa-toolkit")
    except Exception:  # noqa: BLE001 — editable 安装无 metadata 时回落
        toolchain["gqt_version"] = "unknown"
    cfg = root / "addons" / "gut" / "plugin.cfg"
    if cfg.is_file():
        m = _re.search(r'version\s*=\s*"([^"]+)"', cfg.read_text(encoding="utf-8"))
        if m:
            toolchain["gut_version"] = m.group(1)
    try:
        import subprocess as _sp
        r = _sp.run([godot_command(), "--version"], capture_output=True,
                    text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            toolchain["godot_version"] = r.stdout.strip().splitlines()[0]
    except Exception:  # noqa: BLE001 — 版本探测失败保持原值
        pass
    recipe = dict(recipe)
    recipe["toolchain"] = toolchain
    from .recipe import recipe_hash as _recipe_hash
    recipe["recipe_hash"] = _recipe_hash(recipe)
    return recipe


def _load_cached_templates(root: Path, rel: str, src_sha: str,
                           recipe_hash: str, base_algo: str, algo_used: str,
                           test_attribution: bool):
    """静态准备缓存命中（§SPEC-054）：返回
    (registry, probe_manifest, template, template_attr, probe_lines,
    block_map)；未命中或归属模板缺失返回 None（走重新插桩）。"""
    from . import prepcache

    plain_entry = prepcache.load_entry(
        root, rel=rel, src_sha256=src_sha, recipe_hash=recipe_hash,
        algo=base_algo)
    if plain_entry is None:
        return None
    template_attr = None
    if test_attribution:
        attr_entry = prepcache.load_entry(
            root, rel=rel, src_sha256=src_sha, recipe_hash=recipe_hash,
            algo=algo_used)
        if attr_entry is None:
            return None
        template_attr = attr_entry["instrumented_template"]
    return (plain_entry["registry"], plain_entry["probe_manifest"],
            plain_entry["instrumented_template"], template_attr,
            list(plain_entry.get("probe_lines", [])),
            plain_entry.get("block_map") or {})


def _plain_instrument(src: str, tpl_sink: str,
                      block_compress: bool) -> tuple[str, list, dict]:
    """plain 臂（acceptance）插桩：hit 级块压缩或 flag 形态。"""
    if block_compress:
        inst_plain, probe_lines, block_map = _instrument_blocks(
            src, tpl_sink, runtime_sink=True)
    else:
        inst_plain, probe_lines = _instrument(
            src, tpl_sink, flags=True, runtime_sink=True)
        block_map = {}
    return inst_plain, probe_lines, block_map


def _attr_instrument(rel: str, src: str, tpl_sink: str, probe_lines: list,
                     block_compress: bool,
                     attribution_causal: bool) -> str:
    """attr 臂（gut）插桩模板：causal 创建点包装（行号不变，manifest 不受
    影响）+ hit 级块携带归属块——(probe, slot) 边覆盖整个探针面，行事实
    由此展开（fn 级数据布局与 finalizer 不变；attr_probe_ids=None 的旧
    hit 级块布局产品 finalizer 不支持）。"""
    inst_src = src
    if attribution_causal:
        # §SPEC-083 R2：创建点包装（行号不变，manifest 不受影响）
        inst_src, _sites = _rewrite_call_sites(src, file_key8(rel))
    if block_compress:
        attr_probe_ids = frozenset(range(len(probe_lines)))
        return _instrument_blocks(
            inst_src, tpl_sink, runtime_sink=True,
            attribution=True, causal=attribution_causal,
            attr_probe_ids=attr_probe_ids)[0]
    return _instrument(
        inst_src, tpl_sink, flags=True, runtime_sink=True,
        attribution=True, causal=attribution_causal)[0]


def _parse_check(gd_parser, template: str | None,
                 template_attr: str | None) -> str | None:
    """生成期 re-parse 预检（插桩器 bug 必须 fail-loud，不落盘跑假数据）。"""
    for tpl_text in (template, template_attr):
        if tpl_text is None:
            continue
        try:
            gd_parser.parse(tpl_text)
        except Exception as e:  # noqa: BLE001
            return f"instrument_parse_error:{e}"
    return None


def _instrument_pair(rel: str, src: str, registry: dict,
                     block_compress: bool, test_attribution: bool,
                     attribution_causal: bool) -> dict:
    """生成 plain（acceptance 臂）与 attr（gut 臂）插桩模板，并做生成期
    re-parse 预检（插桩器 bug 必须 fail-loud，不落盘跑假数据）。

    返回 {"template","template_attr","probe_lines","block_map"}，或
    {"parse_error": <原因>}（调用方逐文件隔离）。"""
    from gdtoolkit.parser import parser as gd_parser

    from . import prepcache

    tpl_sink = (f"qa-coverage-hits-{prepcache.SINK_UUID_PLACEHOLDER}"
                f"-{file_key8(rel)}.txt")
    template, probe_lines, block_map = _plain_instrument(
        src, tpl_sink, block_compress)
    template_attr = None
    if test_attribution:
        template_attr = _attr_instrument(rel, src, tpl_sink, probe_lines,
                                         block_compress, attribution_causal)
    err = _parse_check(gd_parser, template, template_attr)
    if err is not None:
        return {"parse_error": err}
    return {"template": template, "template_attr": template_attr,
            "probe_lines": probe_lines, "block_map": block_map}


def _store_prepared_templates(root: Path, rel: str, src: str, src_sha: str,
                              recipe_hash: str, base_algo: str,
                              algo_used: str, registry: dict,
                              probe_manifest: dict, template: str,
                              template_attr: str | None,
                              probe_lines: list[int],
                              block_map: dict) -> str | None:
    """回写静态准备缓存；写失败返回原因（只影响下一轮准备成本，不阻断）。"""
    from . import prepcache

    try:
        prepcache.store_entry(
            root, rel=rel, src_sha256=src_sha, recipe_hash=recipe_hash,
            registry=registry, probe_manifest=probe_manifest,
            instrumented_template=template, probe_lines=probe_lines,
            source_plain=src, block_map=block_map, algo=base_algo)
        if template_attr is not None:
            prepcache.store_entry(
                root, rel=rel, src_sha256=src_sha, recipe_hash=recipe_hash,
                registry=registry, probe_manifest=probe_manifest,
                instrumented_template=template_attr,
                probe_lines=probe_lines, source_plain=src,
                block_map=block_map, algo=algo_used)
    except OSError as e:
        return f"cache_write_error:{e}"
    return None


def _causal_site_entry(rel: str, template_attr: str | None) -> dict | None:
    """R2 创建点包装的改写处数（从模板实测，缓存命中路径同样成立）。"""
    if template_attr is None:
        return None
    n_sites = (template_attr.count(".call_deferred_tagged(")
               + template_attr.count(".create_timer_tagged("))
    return {"file": rel, "sites": n_sites} if n_sites else None
def _prepare_fresh(root: Path, rel: str, src: str, path_str: str, *,
                   recipe_hash: str, base_algo: str, algo_used: str,
                   block_compress: bool, test_attribution: bool,
                   attribution_causal: bool,
                   out: dict) -> "tuple | None":
    """缓存未命中路径：registry/manifest（§SPEC-030 单次 parse 纪律）→
    两臂插桩模板 → 回写静态准备缓存（§SPEC-054）。解析/插桩失败返回
    None 并在 out["unmeasured"] 记原因（逐文件隔离，不阻断全集）；缓存
    写失败只记 unmeasured 但条目仍可用（成本损失不阻断测量）。"""
    from ..registry.functions import build_file_registry
    from ..registry.manifest import build_probe_manifest_v2

    try:
        # §SPEC-053：relative-path fid——身份跨 checkout 稳定（绝对
        # 路径会把检出位置混入函数身份）
        registry = build_file_registry(src, file_label=rel)
        probe_manifest = build_probe_manifest_v2(src, registry)
    except Exception as e:  # noqa: BLE001 — 逐文件隔离，不阻断全集
        out["unmeasured"] = {"file": path_str,
                             "reason": f"registry_parse_error:{e}"}
        return None
    built = _instrument_pair(rel, src, registry, block_compress,
                             test_attribution, attribution_causal)
    if "parse_error" in built:
        out["unmeasured"] = {"file": rel, "reason": built["parse_error"]}
        return None
    template, template_attr = built["template"], built["template_attr"]
    probe_lines, block_map = built["probe_lines"], built["block_map"]
    out["cache_misses"] = 1
    write_error = _store_prepared_templates(
        root, rel, src, sha256_text(src), recipe_hash, base_algo, algo_used,
        registry, probe_manifest, template, template_attr, probe_lines,
        block_map)
    if write_error is not None:
        # 缓存写失败不阻断测量（只影响下一轮准备成本）
        out["unmeasured"] = {"file": rel, "reason": write_error}
    return (registry, probe_manifest, template, template_attr,
            probe_lines, block_map)


def _prepare_target_file(root: Path, rel: str, src: str, abs_path: str,
                         path_str: str, *, recipe_hash: str, base_algo: str,
                         algo_used: str, block_compress: bool,
                         test_attribution: bool,
                         attribution_causal: bool) -> dict:
    """单目标文件准备：静态准备缓存 → registry/manifest → 两臂插桩模板。

    §SPEC-030 单次 parse 纪律（模块级只 parse 一次）+ §SPEC-054（缓存命中
    侧跳过全部 parse）+ 方向②③（plain 模板给 acceptance 臂、attr 模板给
    gut 臂，各自独立缓存键）。

    返回 {"prepared", "unmeasured", "cache_hits", "cache_misses",
    "causal_site"}：解析/插桩失败逐文件隔离（prepared=None），缓存写失败
    只记 unmeasured 但条目仍可用（成本损失不阻断测量）。
    """
    out = {"prepared": None, "unmeasured": None,
           "cache_hits": 0, "cache_misses": 0, "causal_site": None}
    src_sha = sha256_text(src)
    cached = _load_cached_templates(
        root, rel, src_sha, recipe_hash, base_algo, algo_used,
        test_attribution)
    if cached is not None:
        (registry, probe_manifest, template, template_attr, probe_lines,
         block_map) = cached
        out["cache_hits"] = 1
    else:
        fresh = _prepare_fresh(
            root, rel, src, path_str, recipe_hash=recipe_hash,
            base_algo=base_algo, algo_used=algo_used,
            block_compress=block_compress,
            test_attribution=test_attribution,
            attribution_causal=attribution_causal, out=out)
        if fresh is None:
            return out
        registry, probe_manifest, template, template_attr, probe_lines, \
            block_map = fresh
    if attribution_causal:
        out["causal_site"] = _causal_site_entry(rel, template_attr)
    out["prepared"] = {
        "abs": abs_path, "src": src,
        "registry": registry, "probe_manifest": probe_manifest,
        "template": template, "template_attr": template_attr,
        "probe_lines": probe_lines,
        "block_map": block_map,
    }
    return out
