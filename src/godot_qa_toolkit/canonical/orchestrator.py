"""gqt canonical 双源批式编排（§SPEC-037，canonical-report/1）。

批式臂（canonical 的唯一可行形态，设计 §2.1）：目标全集一次插桩 → 每臂
1 次 GUT（gut_full 走 .gutconfig 面 + acceptance 私有目录一次）→ 逐文件
sink（`qa-coverage-hits-{run_uuid}-{file_key8}`）读回 → bundle →
coverage-union/1 逐文件行集并集。两臂共用同一 registry/probe manifest
（同源码、同 manifest），run UUID 每臂独立。

在树插桩：备份目录 /tmp/gqt-canonical-backups/<run_uuid>/（started-record
+ 逐文件原文），finally 恢复 + 清理；启动时回收同项目陈旧备份（崩溃恢复，
恢复前不信任任何 sink）。KOL 生产树零残留由「备份+恢复+对账后清理」保证。

certified 边界（§2.4）：一律 evidence_level="exploratory"、
certification.status="not_certified"——单轮 exploratory，WP5 拥有双轮认证。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..accept.run import AcceptRunError, replay_check
from ..accept.suite import (
    SuiteError,
    _cleanup_suite_dir,
    _snapshot_main,
    compile_keys_to_suite,
    create_suite_dir,
    reconcile,
)
from ..coverage.evidence import (
    build_bundle,
    build_run_record,
    clear_sink,
    evidence_dir_for,
    make_run_uuid,
    update_run_record,
    write_bundle,
    write_run_record,
)
from ..coverage.runner import (
    _instrument,
    _instrument_blocks,
    _looks_like_godot_launch_failure,
    _read_hits,
)
from ..coverage.union import arm_record_from_bundle, union_file
from ..mutation.godot_gate import GlobalGodotGate
from ..mutation.inject import godot_command
from .recipe import verify_recipe

REPORT_VERSION = "canonical-report/1"
CERTIFICATION = {
    "status": "not_certified",
    "reason": "single round; WP5 owns two-round certification",
}
_BACKUP_BASE = Path(tempfile.gettempdir()) / "gqt-canonical-backups"

# §SPEC-046：flag 形态的统一落盘由注入的 finalizer autoload 承担——GUT
# 退出（SceneTree 拆卸 _exit_tree）时逐 sink 排空标记数组（每 sink 一次
# open+写循环），落盘次数 = 文件数、与命中事件数无关。仅存在于插桩窗口，
# finally 恢复零残留。
_AUTOLOAD_NAME = "GqtCovFlusher"
_FLUSHER_RELPATH = "addons/gqt_cov_flusher.gd"
_FLUSHER_GD = """extends Node
# qa-toolkit canonical flag-mode probe finalizer (auto-injected, never in
# production). At tree exit, drains every registered per-file flag array to
# its sink: one open + one write loop per file — sink I/O is bounded by file
# count and never grows with hit event count (§SPEC-046).
func _exit_tree() -> void:
\tif not Engine.has_meta("gqt_cov_marks"):
\t\treturn
\tvar d: Dictionary = Engine.get_meta("gqt_cov_marks")
\tfor sink_name in d.keys():
\t\tvar arr: Array = d[sink_name]
\t\tvar f = FileAccess.open("user://" + sink_name, FileAccess.READ_WRITE)
\t\tif f == null:
\t\t\tf = FileAccess.open("user://" + sink_name, FileAccess.WRITE)
\t\telse:
\t\t\tf.seek_end()
\t\tif f == null:
\t\t\tpush_error("gqt coverage probe sink open failed: user://" + sink_name)
\t\t\tcontinue
\t\tfor i in arr.size():
\t\t\tif arr[i] == 1:
\t\t\t\tf.store_line(str(i))
\t\tf.close()
"""


def _patch_autoload(text: str, entry: str) -> str:
    """project.godot 的 [autoload] 段首插入一行 autoload 注册；段缺失则
    追加新段。只动文本结构，不改既有条目。

    置首而非置尾：autoload 退出顺序与加载顺序相反——finalizer 必须最后
    退出，才能排空其他 autoload 在各自 _exit_tree 里产生的关停期命中
    （R5 实证：置尾时 _exit_tree 覆盖整段丢失）。"""
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "[autoload]":
            start = i
            break
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + f"\n[autoload]\n{entry}\n"
    lines.insert(start + 1, entry + "\n")
    return "".join(lines)




class CanonicalError(Exception):
    """canonical 编排入参/前置违例（recipe 非法、目标缺失、GUT 缺失）。"""


def file_key8(relpath: str) -> str:
    return hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:8]


def batch_sink_name(run_uuid: str, relpath: str) -> str:
    return f"qa-coverage-hits-{run_uuid}-{file_key8(relpath)}.txt"


def _recover_stale_backups(project_root: Path) -> int:
    """崩溃恢复：回收本项目的陈旧备份目录（恢复原文、删除备份）。

    恢复前不信任任何 sink——调用方在回收之后才开始新 run。
    """
    restored = 0
    if not _BACKUP_BASE.is_dir():
        return 0
    for d in sorted(_BACKUP_BASE.iterdir()):
        marker = d / "started.json"
        if not marker.is_file():
            continue
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("project_root") != str(project_root):
            continue
        for src, original in record.get("files", {}).items():
            Path(src).write_text(original, encoding="utf-8")
        pg = record.get("project_godot")
        if pg is not None:
            (project_root / "project.godot").write_text(pg, encoding="utf-8")
        for created in record.get("created", []):
            Path(created).unlink(missing_ok=True)
        shutil.rmtree(d, ignore_errors=True)
        restored += 1
    return restored


def _run_one_gut(project_root: Path, gut_args: list[str],
                 timeout_s: int,
                 env: dict | None = None,
                 gate_root: Path | None = None
                 ) -> tuple[subprocess.CompletedProcess, float]:
    t0 = time.monotonic()
    gut = project_root / "addons" / "gut" / "gut_cmdln.gd"
    if not gut.is_file():
        raise CanonicalError(f"GUT runner not found: {gut}")
    argv = [godot_command(), "--headless", "--path", str(project_root),
            "-s", "res://addons/gut/gut_cmdln.gd", *gut_args, "-gexit"]
    with GlobalGodotGate(str(gate_root or project_root)):
        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               timeout=timeout_s, cwd=str(project_root),
                               env=env)
        except subprocess.TimeoutExpired as e:
            raise CanonicalError(
                f"GUT timed out after {timeout_s}s "
                f"(args: {' '.join(gut_args)})") from e
    return r, time.monotonic() - t0


def _project_name(root: Path) -> str:
    pg = root / "project.godot"
    if pg.is_file():
        m = re.search(r'config/name\s*=\s*"([^"]+)"',
                      pg.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    return root.name


def _shard_groups(root: Path, dirs: list[str], width: int) -> list[list[str]]:
    """按测试文件数 LPT 装箱把 config 目录分到 width 组（§SPEC-052 并行
    分片臂）；weight 用 test_*.gd 文件数近似执行成本。"""
    if not dirs:
        return [[]]
    if width <= 1:
        return [list(dirs)]
    weighted = []
    for d in dirs:
        rel = d[len("res://"):] if d.startswith("res://") else d
        p = root / rel
        w = len(list(p.rglob("test_*.gd"))) if p.is_dir() else 1
        weighted.append((max(1, w), d))
    n = min(width, len(weighted))
    groups: list[list[str]] = [[] for _ in range(n)]
    loads = [0] * n
    for w, d in sorted(weighted, key=lambda x: -x[0]):
        i = loads.index(min(loads))
        groups[i].append(d)
        loads[i] += w
    return [g for g in groups if g]


def _prepare_shadow(base_root: Path, run_uuid: str, idx: int) -> Path:
    """§SPEC-056：分片独立 project shadow——源码/resource 状态与基准树
    完全一致（hardlink farm，零拷贝成本），`.godot` 引擎缓存实拷贝隔离，
    杜绝多 Godot 进程争用同一项目目录缓存。junit 硬链断开放分片自写。"""
    import subprocess as _sp

    shadow = Path(tempfile.mkdtemp(
        prefix=f"gqt-shadow-{run_uuid[:8]}-{idx}-"))
    entries = [e for e in base_root.iterdir() if e.name != ".godot"]
    for e in entries:
        _sp.run(["cp", "-al", str(e), str(shadow / e.name)], check=True)
    godot_cache = base_root / ".godot"
    if godot_cache.is_dir():
        _sp.run(["cp", "-a", str(godot_cache),
                 str(shadow / ".godot")], check=True)
    junit = shadow / "tests" / "reports" / "junit_report.xml"
    if junit.is_file():
        junit.unlink()
    return shadow


def _cleanup_shadow(shadow: Path | None) -> None:
    if shadow is not None:
        shutil.rmtree(shadow, ignore_errors=True)


def _shard_user_dir(xdg_home: str, project_name: str) -> Path:
    return Path(xdg_home) / "godot" / "app_userdata" / project_name


def _read_hits_merged(root: Path, run_uuid: str, rel: str,
                      manifest: list[int],
                      xdg_homes: list[str],
                      retain_dir: Path | None = None
                      ) -> tuple[set[int], int]:
    """并行分片臂的合并读回：sink 名含进程 PID（`...-<uuid>-<pid>-<key8>`
    .txt），按 run_uuid+key8 模式跨全部分片 user:// 目录取并集。"""
    key8 = file_key8(rel)
    name = f"qa-coverage-hits-{run_uuid}-*-{key8}.txt"
    proj = _project_name(root)
    ids: list[int] = []
    files: list[Path] = []
    for xdg in xdg_homes:
        d = _shard_user_dir(xdg, proj)
        if d.is_dir():
            files.extend(sorted(d.glob(name)))
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
            if retain_dir is not None:
                retain_dir.mkdir(parents=True, exist_ok=True)
                (retain_dir / f.name).write_text(text, encoding="utf-8")
            for ln in text.splitlines():
                ln = ln.strip()
                if ln.isdigit():
                    ids.append(int(ln))
        finally:
            f.unlink(missing_ok=True)
    valid = set(range(len(manifest)))
    hits = {manifest[pid] for pid in ids if pid in valid}
    return hits, sum(1 for pid in ids if pid in valid)


def _inject_toolchain(root: Path, recipe: dict) -> dict:
    """PR-A L1 预留的编排器职责：测量时注入真实 toolchain 版本并重算
    recipe_hash（注入路径下版本变化即 recipe 变化）。加载态先 verify
    （防篡改），注入后重 hash 再作为测量身份。"""
    import re as _re

    from ..accept import OPS_VERSION
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


def run_canonical(project_root: str | Path, target_files: list[str],
                  recipe: dict, *, acceptance_keys: list[str] | None = None,
                  probe_keys: list[str] | None = None, timeout_s: int = 180,
                  evidence_out: str | None = None,
                  inject_toolchain: bool = True,
                  scope_roots: list[str] | None = None,
                  block_compress: bool = True,
                  shard_width: int = 6,
                  acceptance_shards: int = 4,
                  shard_log_dir: str | None = None,
                  stability_baseline: str | None = None) -> dict:
    """canonical 双源批式编排，产出 canonical-report/1。"""
    t_command = time.monotonic()
    root = Path(project_root).resolve()
    ok, reason = verify_recipe(recipe)
    if not ok:
        raise CanonicalError(f"recipe invalid: {reason}")
    if not target_files:
        raise CanonicalError("no target files given")
    if inject_toolchain:
        recipe = _inject_toolchain(root, recipe)
    recipe_hash = recipe["recipe_hash"]
    t_gated = time.monotonic()
    evidence_dir = evidence_dir_for(evidence_out) if evidence_out else None
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)

    # 目标全集：registry + probe manifest + 插桩模板每文件只建一次
    # （§SPEC-030 单次 parse 纪律；两臂共用同一模板——同源码同 manifest 的
    # union 前提）。§SPEC-054：命中静态准备缓存则跳过全部 parse；模板以
    # uuid 占位符保存（臂间仅占位替换）。parse 失败逐文件隔离进 unmeasured。
    from ..registry.functions import build_file_registry
    from ..registry.manifest import build_probe_manifest_v2
    from gdtoolkit.parser import parser as _gd_parser

    from . import prepcache

    prepared: dict[str, dict] = {}
    unmeasured: list[dict] = []
    cache_hits = 0
    cache_misses = 0
    for path_str in target_files:
        p = Path(path_str)
        if not p.is_file():
            unmeasured.append({"file": path_str, "reason": "target_missing"})
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            unmeasured.append({"file": path_str,
                               "reason": f"read_error:{e}"})
            continue
        rel = str(p.resolve().relative_to(root))
        src_sha = hashlib.sha256(src.encode("utf-8")).hexdigest()
        prep_algo = (prepcache.PROBE_ALGO_VERSION if block_compress
                     else "flags/4-pid")
        entry = prepcache.load_entry(
            root, rel=rel, src_sha256=src_sha, recipe_hash=recipe_hash,
            algo=prep_algo)
        if entry is not None:
            registry = entry["registry"]
            probe_manifest = entry["probe_manifest"]
            template = entry["instrumented_template"]
            probe_lines = list(entry.get("probe_lines", []))
            block_map = entry.get("block_map") or {}
            cache_hits += 1
        else:
            try:
                # §SPEC-053：relative-path fid——身份跨 checkout 稳定（绝对
                # 路径会把检出位置混入函数身份）
                registry = build_file_registry(src, file_label=rel)
                probe_manifest = build_probe_manifest_v2(src, registry)
            except Exception as e:  # noqa: BLE001 — 逐文件隔离，不阻断全集
                unmeasured.append({"file": path_str,
                                   "reason": f"registry_parse_error:{e}"})
                continue
            tpl_sink = (f"qa-coverage-hits-{prepcache.SINK_UUID_PLACEHOLDER}"
                        f"-{file_key8(rel)}.txt")
            if block_compress:
                inst0, probe_lines, block_map = _instrument_blocks(
                    src, tpl_sink, runtime_sink=True)
            else:
                inst0, probe_lines = _instrument(
                    src, tpl_sink, flags=True, runtime_sink=True)
                block_map = {}
            if probe_lines:
                # 生成期 re-parse 预检：插桩产物不合法 = 插桩器 bug →
                # 逐文件隔离，拒绝落盘跑假数据（缓存命中侧由校验和守卫）
                try:
                    _gd_parser.parse(inst0)
                except Exception as e:  # noqa: BLE001
                    unmeasured.append(
                        {"file": rel,
                         "reason": f"instrument_parse_error:{e}"})
                    continue
            template = inst0
            cache_misses += 1
            try:
                prepcache.store_entry(
                    root, rel=rel, src_sha256=src_sha,
                    recipe_hash=recipe_hash, registry=registry,
                    probe_manifest=probe_manifest,
                    instrumented_template=template,
                    probe_lines=probe_lines, source_plain=src,
                    block_map=block_map, algo=prep_algo)
            except OSError as e:
                # 缓存写失败不阻断测量（只影响下一轮准备成本）
                unmeasured.append({"file": rel,
                                   "reason": f"cache_write_error:{e}"})
        prepared[rel] = {
            "abs": str(p.resolve()), "src": src,
            "registry": registry, "probe_manifest": probe_manifest,
            "template": template, "probe_lines": probe_lines,
            "block_map": block_map,
        }

    # §SPEC-053：subset 不得标 comparable——目标集未覆盖 scope_roots 下全部
    # .gd 时，union/评分的 comparable 一律降为 false 并给原因。
    scope_complete = True
    if scope_roots:
        scope_files = {str(x) for sr in scope_roots
                       for x in Path(sr).rglob("*.gd") if x.is_file()}
        scope_complete = scope_files <= {str(Path(tf)) for tf in target_files}
    recovered = _recover_stale_backups(root)


    def _arm(name: str, acceptance: dict | None = None) -> dict:
        """一条批式臂（§SPEC-052 并行分片）：复用全集插桩 → 按目录/feature
        组并行扇出 N 个 GUT 进程（每进程独立 XDG user:// 隔离 + 运行时
        PID sink）→ 合并逐文件 sink 读回 → bundle → 恢复/清理。零可执行行
        文件进显式 vacuous 车道（§SPEC-042）。"""
        from concurrent.futures import ThreadPoolExecutor

        run_uuid = make_run_uuid()
        backup_dir = _BACKUP_BASE / run_uuid
        backup_dir.mkdir(parents=True)
        started = {"run_uuid": run_uuid, "project_root": str(root),
                   "files": {}}
        sink_by_rel: dict[str, str] = {}
        manifest_by_rel: dict[str, list[int]] = {}
        instrumented_files: dict[str, str] = {}
        vacuous_rels: list[str] = []
        for rel, item in prepared.items():
            template = item.get("template")
            manifest = item.get("probe_lines") or []
            if template is None:
                continue  # 插桩失败已隔离进 unmeasured
            if not manifest:
                vacuous_rels.append(rel)
                continue
            sink = batch_sink_name(run_uuid, rel)
            n_ph = template.count(prepcache.SINK_UUID_PLACEHOLDER)
            # §SPEC-054：模板以 uuid 占位符保存，臂间仅做占位替换
            inst = template.replace(prepcache.SINK_UUID_PLACEHOLDER,
                                    str(run_uuid))
            if not n_ph:
                raise CanonicalError(
                    f"sink placeholder missing for {rel} (stale cache?)")
            started["files"][item["abs"]] = item["src"]
            sink_by_rel[rel] = sink
            manifest_by_rel[rel] = manifest
            instrumented_files[item["abs"]] = inst
        pg_path = root / "project.godot"
        if not pg_path.is_file():
            raise CanonicalError(
                "cannot inject finalizer autoload: project.godot missing")
        started["project_godot"] = pg_path.read_text(encoding="utf-8")
        started["created"] = [str(root / _FLUSHER_RELPATH)]
        (backup_dir / "started.json").write_text(
            json.dumps(started, ensure_ascii=False), encoding="utf-8")
        # 分片计划：acceptance 组由调用方编译；gut_full 按 gutconfig dirs
        if acceptance is not None:
            plans = [
                {"args": ["-gdir=res://" + Path(g["suite_dir"]).relative_to(
                    root).as_posix() + "/generated"],
                 "keys": g["keys"], "suite_dir": Path(g["suite_dir"])}
                for g in acceptance["groups"]]
        else:
            groups = _shard_groups(
                root, list(recipe["gut_face"].get("dirs") or []),
                shard_width)
            plans = [{"args": ([("-gdir=" + ",".join(g))] if g else []),
                      "keys": None, "suite_dir": None} for g in groups]
        gut_ok = True
        run_error = False
        timed_out = False
        output = ""
        results: list[dict] = []
        xdg_homes: list[str] = []

        def _run_shard(ip):
            idx, plan = ip
            xdg = tempfile.mkdtemp(
                prefix=f"gqt-shard-{run_uuid[:8]}-{idx}-")
            env = dict(os.environ)
            env["XDG_DATA_HOME"] = xdg
            shadow = None
            try:
                # §SPEC-056：分片独立 project shadow（独立 .godot）
                shadow = _prepare_shadow(root, run_uuid, idx)
                r, _w = _run_one_gut(shadow, plan["args"], timeout_s,
                                     env=env, gate_root=root)
                out = (r.stdout or "") + "\n" + (r.stderr or "")
                rc = r.returncode
                timed_out = False
            except CanonicalError as e:
                te = getattr(e, "__cause__", None)
                tail = ""
                if te is not None:
                    tail = ((getattr(te, "stdout", "") or "") + "\n" +
                            (getattr(te, "stderr", "") or ""))
                out = str(e) + "\n" + tail
                rc = None
                timed_out = True
            finally:
                _cleanup_shadow(shadow)
            if shard_log_dir:
                log_dir = Path(shard_log_dir)
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / f"{name}-shard{idx}.log").write_text(
                    out, encoding="utf-8")
            return {"plan": plan, "xdg": xdg, "rc": rc,
                    "out": out[-6000:] if timed_out else out,
                    "timed_out": timed_out}

        try:
            for abs_path, inst in instrumented_files.items():
                Path(abs_path).write_text(inst, encoding="utf-8")
            pg_path.write_text(
                _patch_autoload(
                    started["project_godot"],
                    f'{_AUTOLOAD_NAME}="*res://{_FLUSHER_RELPATH}"'),
                encoding="utf-8")
            flusher_abs = root / _FLUSHER_RELPATH
            flusher_abs.parent.mkdir(parents=True, exist_ok=True)
            flusher_abs.write_text(_FLUSHER_GD, encoding="utf-8")
            t_arm = time.monotonic()
            with ThreadPoolExecutor(max_workers=max(1, len(plans))) as ex:
                results = list(ex.map(_run_shard, enumerate(plans)))
            wall_s = time.monotonic() - t_arm
            xdg_homes = [r["xdg"] for r in results]
            for r in results:
                if r["timed_out"]:
                    gut_ok = False
                    run_error = True
                    timed_out = True
                elif r["rc"] != 0 and _looks_like_godot_launch_failure(
                        r["out"]):
                    gut_ok = False
                    run_error = True
            output = "\n".join(r["out"] for r in results)
        finally:
            for abs_path, original in started["files"].items():
                Path(abs_path).write_text(original, encoding="utf-8")
            if "project_godot" in started:
                pg_path.write_text(started["project_godot"], encoding="utf-8")
            for created in started.get("created", []):
                Path(created).unlink(missing_ok=True)
            shutil.rmtree(backup_dir, ignore_errors=True)

        reconciliation = None
        arm_valid = not run_error
        invalid_reason = ("timeout" if timed_out
                          else ("run_error" if run_error else None))
        if acceptance is not None and not run_error:
            # §SPEC-035 对账：分片组逐个对账，任一 false → 臂 invalid
            recs = []
            ok_all = True
            for r in results:
                rec = reconcile(
                    root, r["plan"]["suite_dir"], r["plan"]["keys"],
                    r["out"], replay_ok=True,
                    main_snapshot=acceptance["snapshot"])
                recs.append(rec)
                ok_all = ok_all and bool(rec.get("ok"))
            reconciliation = {"ok": ok_all, "shards": recs}
            if not ok_all:
                arm_valid = False
                invalid_reason = "reconciliation_failed"
        if acceptance is not None:
            for r in results:
                if r["plan"].get("suite_dir") is not None:
                    _cleanup_suite_dir(Path(r["plan"]["suite_dir"]))

        bundles: dict[str, dict] = {}
        for rel, sink in sink_by_rel.items():
            item = prepared[rel]
            retain = (Path(shard_log_dir) / f"{name}-sinks"
                      if shard_log_dir else None)
            hits, probes_fired = _read_hits_merged(
                root, run_uuid, rel, manifest_by_rel[rel], xdg_homes,
                retain_dir=retain)
            block_map = prepared[rel].get("block_map") or {}
            if block_map:
                # §SPEC-055：块入口命中 → 块内全部语句起始行
                hits = {ln for start in hits
                        for ln in block_map.get(start, [start])}
            bundle = build_bundle(
                run_uuid=run_uuid, file_path="res://" + rel,
                gd_source=item["src"], tests_glob=None,
                registry=item["registry"],
                probe_manifest=item["probe_manifest"],
                hits_lines=sorted(hits), probes_fired=probes_fired,
                gut_ok=gut_ok, run_error=run_error, timeout=timed_out,
                zero_tests=False, vacuous=False, arm=name)
            bundles[rel] = bundle
            if evidence_dir is not None:
                # 批式同臂共享 run_uuid——evidence 文件名必须带 per-file
                # 后缀防互相覆盖（R3 实证缺陷）
                suffix = f"-{file_key8(rel)}"
                record = build_run_record(
                    run_uuid=run_uuid, file_path="res://" + rel,
                    gd_source=item["src"], tests_glob=None,
                    probe_manifest=item["probe_manifest"], arm=name)
                record["sink_path"] = str(evidence_dir / sink)
                write_run_record(evidence_dir, record, name_suffix=suffix)
                write_bundle(evidence_dir, bundle, name_suffix=suffix)
                update_run_record(evidence_dir, run_uuid, name_suffix=suffix,
                                  status="bundle_written")
            clear_sink(None, str(root), sink)
        for xdg in xdg_homes:
            shutil.rmtree(xdg, ignore_errors=True)
        for rel in vacuous_rels:
            # §SPEC-042：零可执行行 → 显式 vacuous 车道（bundle 落盘、
            # union 按 §SPEC-036 严格语义仍 invalid，reason=vacuous）
            item = prepared[rel]
            bundle = build_bundle(
                run_uuid=run_uuid, file_path="res://" + rel,
                gd_source=item["src"], tests_glob=None,
                registry=item["registry"],
                probe_manifest=item["probe_manifest"],
                hits_lines=[], probes_fired=0,
                gut_ok=gut_ok, run_error=run_error, timeout=timed_out,
                zero_tests=False, vacuous=True, arm=name)
            bundles[rel] = bundle
            if evidence_dir is not None:
                suffix = f"-{file_key8(rel)}"
                record = build_run_record(
                    run_uuid=run_uuid, file_path="res://" + rel,
                    gd_source=item["src"], tests_glob=None,
                    probe_manifest=item["probe_manifest"], arm=name)
                write_run_record(evidence_dir, record, name_suffix=suffix)
                write_bundle(evidence_dir, bundle, name_suffix=suffix)
                update_run_record(evidence_dir, run_uuid, name_suffix=suffix,
                                  status="bundle_written")
        return {
            "arm": name, "run_uuid": run_uuid, "wall_s": round(wall_s, 3),
            "gut_ok": gut_ok, "timed_out": timed_out,
            "state": ("valid" if arm_valid else "invalid"),
            "invalid_reason": invalid_reason,
            "bundles": len(bundles), "bundles_data": bundles,
            "reconciliation": reconciliation,
            "shards": len(plans),
            "shadowed": True,
        }

    # 臂 gut_full：.gutconfig 面（无 -gdir——GUT 缺省读配置，lab 实测锚定）
    arm_a = _arm("gut_full")

    # 臂 acceptance：私有 suite 按分片分组编译/重放 → 并行扇出 GUT
    keys = list(acceptance_keys) if acceptance_keys else [
        f["key"] for f in recipe["acceptance_face"]["features"]]
    snapshot = _snapshot_main(root)
    groups: list[dict] = []
    try:
        n_groups = max(1, min(int(acceptance_shards or 1), len(keys) or 1))
        chunk = max(1, -(-len(keys) // n_groups))
        for i in range(0, len(keys), chunk):
            sub = keys[i:i + chunk]
            sd = create_suite_dir(root)
            groups.append({"suite_dir": sd, "keys": sub})
            compile_keys_to_suite(root, sub, sd)
            for k in sub:
                replay_check(root, k, suite_dir=sd)
        arm_b = _arm("acceptance", acceptance={
            "groups": groups, "snapshot": snapshot})
    except (SuiteError, AcceptRunError) as e:
        for g in groups:
            _cleanup_suite_dir(Path(g["suite_dir"]))
        arm_b = {"arm": "acceptance", "run_uuid": None, "wall_s": 0.0,
                 "gut_ok": False, "timed_out": False, "state": "invalid",
                 "invalid_reason": f"suite_error:{e}", "bundles": 0,
                 "bundles_data": {}, "reconciliation": None}

    # 逐文件 union（coverage-union/1，禁平均）
    unions = []
    for rel in sorted(set(arm_a["bundles_data"])
                      & set(arm_b["bundles_data"])):
        a = arm_record_from_bundle(arm_a["bundles_data"][rel],
                                   recipe_hash=recipe_hash,
                                   wall_s=arm_a["wall_s"])
        b = arm_record_from_bundle(arm_b["bundles_data"][rel],
                                   recipe_hash=recipe_hash,
                                   wall_s=arm_b["wall_s"])
        unions.append(union_file(a, b, canonical_recipe_hash=recipe_hash))
    union_valid = bool(unions) and all(u["valid"] for u in unions)
    comparable_reason = None
    if not scope_complete:
        comparable_reason = "subset_targets"
        for u in unions:
            u["comparable"] = False
            u["comparable_reason"] = "subset_targets"
    baseline_lines = (_load_baseline_lines(stability_baseline)
                      if stability_baseline else {})
    unstable_keys = (_unstable_keys(unions, baseline_lines)
                     if baseline_lines else set())
    crap_records, crap_lanes, crap_version = _crap_records_for_report(
        unions, prepared, recipe_hash, unstable_keys)
    # §SPEC-043 measurable 子集披露：vacuous / no_probe_fired 文件归
    # unmeasurable（本就无可测数据），其余 union 无效仍属 measurable 失败
    # （超时/契约违约等），禁止静默排除——measurable + unmeasurable == 全集。
    unmeasurable: list[dict] = []
    measurable_files = 0
    measurable_valid = 0
    for u in unions:
        if u["valid"]:
            measurable_files += 1
            measurable_valid += 1
            continue
        reason = u.get("invalid_reason") or ""
        if "no_probe_fired" in reason or "vacuous" in reason:
            unmeasurable.append({"file": u["file"], "reason": reason})
        else:
            measurable_files += 1
    # §SPEC-039 门量：插桩 + 两臂 GUT + union（recipe 构建与 W4 探针不计）
    gated_core_s = round(time.monotonic() - t_gated, 3)
    perf = _performance(root, probe_keys, timeout_s)
    perf["gated_core_s"] = gated_core_s
    perf["command_total_s"] = round(time.monotonic() - t_command, 3)
    return {
        "report_version": REPORT_VERSION,
        "project_root": str(root),
        "recipe_hash": recipe_hash,
        "arms": [
            {k: v for k, v in arm.items() if k != "bundles_data"}
            for arm in (arm_a, arm_b)
        ],
        "unions": unions,
        "union": {
            "valid": union_valid,
            "files": len(unions),
            "valid_files": sum(1 for u in unions if u["valid"]),
            "method": "line_union",
            "measurable": {
                "files": measurable_files,
                "valid": measurable_valid,
                "all_valid": (bool(measurable_files)
                              and measurable_valid == measurable_files),
            },
            "unmeasurable": unmeasurable,
        },
        "crap": {
            "version": crap_version,
            "evidence_level": "exploratory",
            "coverage_semantics": "statement_start_line",
            "path_coverage": "not_measured",
            "assertion_effectiveness": "not_measured",
            "method": "coverage-union/1 + func-registry",
            "records": crap_records,
            "summary": {
                "functions": len(crap_records),
                "scored": sum(1 for r in crap_records
                              if r["score_status"] == "scored"),
                "lanes": crap_lanes,
            },
            "comparable": scope_complete,
            "comparable_reason": comparable_reason,
            "stability": {
                "baseline": stability_baseline,
                "unstable_functions": len(unstable_keys),
            },
        },
        "preparation": {
            "cache": {"hits": cache_hits, "misses": cache_misses,
                      "algo": (prepcache.PROBE_ALGO_VERSION
                               if block_compress else "flags/4")},
        },
        "unmeasured": unmeasured,
        "recovered_stale_backups": recovered,
        "performance": perf,
        "certification": dict(CERTIFICATION),
        "evidence_level": "exploratory",
        "diagnostic_only": True,
        "dynamic_cache": False,
        "ok": union_valid,
    }


_FAIL_LANES = ("timeout", "run_error", "vacuous", "no_probe_fired",
               "zero_tests", "suite_invalid")


def _lane_from_reason(reason: str | None) -> str:
    """union 无效原因 → 失败车道。失败样本一律 not-scored（§SPEC-053）。"""
    for lane in _FAIL_LANES:
        if reason and lane in reason:
            return lane
    return "invalid"


def _crap_records_for_report(unions, prepared, recipe_hash,
                             unstable_keys: set | None = None):
    """§SPEC-053：函数级 CRAP 记录——union 行集（C/I）与 registry 的
    identity/CC 按 fid 连接；任何失败/无分母车道一律 score=None。"""
    from ..risk.contract import build_shadow_record
    from ..risk.formula import CRAP_LINE_VERSION

    by_file = {u["file"]: u for u in unions}
    records: list[dict] = []
    lanes: dict[str, int] = {}

    unstable_keys = unstable_keys or set()

    def _emit(rel: str, entry: dict, *, state: str,
              covered: int | None = None, total: int | None = None) -> None:
        scope = entry.get("lex_scope") or []
        records.append(build_shadow_record(
            function_id=entry["fid"],
            name=entry.get("name", ""),
            lex_path=(".".join(str(x) for x in scope)
                      or entry.get("name", "")),
            start_line=int(entry.get("start_line") or 0),
            end_line=int(entry.get("end_line") or 0),
            # registry 的 CC 字段名为 cc_oracle（§SPEC-053 连接契约）；
            # 缺失即 cc=0 会产出伪零分，必须显式失败
            cc=int(entry["cc_oracle"]),
            covered=covered, total=total,
            cov_source=("canonical_union" if state == "scored"
                        else "not_measured"),
            state=state,
            recipe={"recipe_hash": recipe_hash, "method": "canonical-union/1"},
            lambda_gap=int(entry.get("lambda_gap") or 0),
        ))
        lanes[state] = lanes.get(state, 0) + 1

    for rel, item in prepared.items():
        entries = item["registry"].get("entries", [])
        u = by_file.get("res://" + rel)
        if u is None or not u.get("valid"):
            lane = (_lane_from_reason(u.get("invalid_reason"))
                    if u is not None else "unknown")
            for e in entries:
                _emit(rel, e, state=lane)
            continue
        per = {row["fid"]: row for row in u["union"]["per_fid"]}
        for e in entries:
            row = per.get(e["fid"])
            if row is None or int(row.get("total") or 0) <= 0:
                # 缺分母/未入 probe 面：unknown 不出分（不冒充 100%）
                _emit(rel, e, state="unknown")
            elif ("res://" + rel, e["fid"]) in unstable_keys:
                # §SPEC-056：跨 run 抖动函数 → 显式 unstable 车道，不出分
                _emit(rel, e, state="unstable",
                      covered=int(row["covered"]),
                      total=int(row["total"]))
            else:
                _emit(rel, e, state="scored",
                      covered=int(row["covered"]),
                      total=int(row["total"]))
    return records, lanes, CRAP_LINE_VERSION


def _load_baseline_lines(path: str) -> dict:
    """§SPEC-056 stability baseline：读取历史 canonical 报告的逐 fid 行集。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CanonicalError(
            f"stability baseline unreadable: {path}: {e}") from e
    out: dict = {}
    for u in doc.get("unions") or []:
        if u.get("union") is None:
            continue
        out[u["file"]] = {row["fid"]: frozenset(row["covered_line_ids"])
                          for row in u["union"].get("per_fid") or []}
    return out


def _unstable_keys(unions, baseline_lines: dict) -> set:
    keys: set = set()
    for u in unions:
        if u.get("union") is None:
            continue
        base = baseline_lines.get(u["file"], {})
        for row in u["union"].get("per_fid") or []:
            fid = row["fid"]
            if fid in base and base[fid] != frozenset(row["covered_line_ids"]):
                keys.add((u["file"], fid))
    return keys


def _safe_log_label(label: str) -> str:
    """日志文件名消毒：组跑（"k1+k2+..."）会超文件名上限。"""
    if len(label) <= 48:
        return label
    return hashlib.sha1(label.encode()).hexdigest()[:12]


DIAGNOSTIC_VERSION = "canonical-diagnostic/1"

# §SPEC-048：wait 步骤静态识别（feature 文本）——duration 等待与
# `wait until ...` 条件等待都属于 player_session 的墙钟竞争路径。
_WAIT_STEP_RE = re.compile(r'agent (?:执行 "wait[^"]*"|等待[^\n]*)')

# §SPEC-048：GUT 汇总行解析（诊断双腿共用的输出摘要）。
_DIAG_GUT_SUMMARY_RES = {
    "scripts": re.compile(r"^Scripts\s+(\d+)$", re.M),
    "tests": re.compile(r"^Tests\s+(\d+)$", re.M),
    "passing": re.compile(r"^Passing Tests\s+(\d+)$", re.M),
    "failing": re.compile(r"^Failing Tests\s+(\d+)$", re.M),
    "risky": re.compile(r"^Risky/Pending\s+(\d+)$", re.M),
    "gut_time": re.compile(r"^Time\s+([0-9.]+)s$", re.M),
}


def _dump_progress(report: dict, progress_out: str | None) -> None:
    """诊断进度落盘：每 feature 完成即写 progress_out（长跑分段留证）。"""
    if progress_out:
        Path(progress_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1),
            encoding="utf-8")


def _parse_gut_summary(output: str) -> dict:
    parsed = {}
    for name, rx in _DIAG_GUT_SUMMARY_RES.items():
        m = rx.search(output)
        parsed[name] = (float(m.group(1)) if name == "gut_time"
                        else int(m.group(1))) if m else None
    return parsed


def _diagnose_prepare(recipe: dict, feature_keys: list[str],
                      target_files: list[str]) -> tuple[dict, dict[str, str]]:
    """校验 recipe 与 feature keys，读取目标源码；失败即 raise
    CanonicalError（与编排器其他入口同款 fail-loud 语义）。"""
    ok, reason = verify_recipe(recipe)
    if not ok:
        raise CanonicalError(f"recipe invalid: {reason}")
    feature_by_key = {f["key"]: f
                      for f in recipe["acceptance_face"]["features"]}
    unknown = [k for spec in feature_keys for k in spec.split("+")
               if k and k not in feature_by_key]
    if unknown:
        raise CanonicalError(f"unknown acceptance features: {unknown}")
    sources: dict[str, str] = {}
    for path_str in target_files:
        p = Path(path_str)
        try:
            sources[str(p.resolve())] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise CanonicalError(f"target unreadable: {p}: {e}") from e
    return feature_by_key, sources


def _diagnose_feature_text(root: Path, feature_by_key: dict,
                           sub_keys: list[str]) -> str:
    feature_text = ""
    for sk in sub_keys:
        src = feature_by_key[sk].get("source", "")
        if isinstance(src, str) and src.startswith("res://"):
            fpath = root / src[len("res://"):]
            if fpath.is_file():
                feature_text += fpath.read_text(encoding="utf-8")
    return feature_text


def _diagnose_baseline_leg(root: Path, gut_args: list[str],
                           timeout_s: int, progress_out: str | None,
                           key: str) -> dict:
    """腿 1：未插桩基线（同题编译套件直跑），附基线腿日志落盘。"""
    t0 = time.monotonic()
    r, _ = _run_one_gut(root, gut_args, timeout_s)
    wall = time.monotonic() - t0
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    if progress_out:
        Path(f"{progress_out}.{_safe_log_label(key)}.baseline.log"
             ).write_text(out, encoding="utf-8")
    return {"wall_s": round(wall, 3), "rc": r.returncode,
            "tests": _parse_gut_summary(out)}


def _diagnose_stage_flags(root: Path, sources: dict[str, str],
                          run_uuid: str) -> tuple[Path, dict, dict[str, str],
                                                  dict[str, list[int]],
                                                  dict[str, str]]:
    """腿 2 备份与插桩：备份目录落 started.json，返回 (backup_dir,
    started, sink_by_rel, manifest_by_rel, instrumented)。零可执行行文件
    不入插桩面（vacuous 不写不还原）。"""
    backup_dir = _BACKUP_BASE / run_uuid
    backup_dir.mkdir(parents=True)
    started = {"run_uuid": run_uuid, "project_root": str(root),
               "files": {}}
    sink_by_rel: dict[str, str] = {}
    manifest_by_rel: dict[str, list[int]] = {}
    instrumented: dict[str, str] = {}
    for abs_path, src_text in sources.items():
        rel = str(Path(abs_path).relative_to(root))
        sink = batch_sink_name(run_uuid, rel)
        inst, manifest = _instrument(src_text, sink, flags=True)
        if not manifest:
            continue
        started["files"][abs_path] = src_text
        sink_by_rel[rel] = sink
        manifest_by_rel[rel] = manifest
        instrumented[abs_path] = inst
    pg_path = root / "project.godot"
    started["project_godot"] = pg_path.read_text(encoding="utf-8")
    started["created"] = [str(root / _FLUSHER_RELPATH)]
    (backup_dir / "started.json").write_text(
        json.dumps(started, ensure_ascii=False), encoding="utf-8")
    return backup_dir, started, sink_by_rel, manifest_by_rel, instrumented


def _diagnose_run_flags_leg(root: Path, gut_args: list[str],
                            timeout_s: int, backup_dir: Path,
                            started: dict,
                            instrumented: dict[str, str]
                            ) -> tuple[bool, str, int | None, float]:
    """写插桩文件、注入 finalizer、跑 GUT；finally 恢复原树后返回
    (timed_out, 合并输出, rc, 墙钟秒)。GUT 超时按 CanonicalError 带因
    捕获、不向上传播（归因腿仍需读 sink 残余）。"""
    timed_out = False
    out = ""
    rc = None
    wall = 0.0
    pg_path = root / "project.godot"
    try:
        for abs_path, inst in instrumented.items():
            Path(abs_path).write_text(inst, encoding="utf-8")
        pg_path.write_text(
            _patch_autoload(
                started["project_godot"],
                f'{_AUTOLOAD_NAME}="*res://{_FLUSHER_RELPATH}"'),
            encoding="utf-8")
        flusher_abs = root / _FLUSHER_RELPATH
        flusher_abs.parent.mkdir(parents=True, exist_ok=True)
        flusher_abs.write_text(_FLUSHER_GD, encoding="utf-8")
        t0 = time.monotonic()
        try:
            r, _ = _run_one_gut(root, gut_args, timeout_s)
            wall = time.monotonic() - t0
            rc = r.returncode
            out = (r.stdout or "") + "\n" + (r.stderr or "")
        except CanonicalError as e:
            timed_out = True
            wall = time.monotonic() - t0
            te = getattr(e, "__cause__", None)
            if te is not None:
                out = (getattr(te, "stdout", "") or "") + "\n" + (
                    getattr(te, "stderr", "") or "")
    finally:
        for abs_path, original in started["files"].items():
            Path(abs_path).write_text(original, encoding="utf-8")
        pg_path.write_text(started["project_godot"],
                           encoding="utf-8")
        for created in started.get("created", []):
            Path(created).unlink(missing_ok=True)
        shutil.rmtree(backup_dir, ignore_errors=True)
    return timed_out, out, rc, wall


def _diagnose_collect_hits(root: Path, sink_by_rel: dict[str, str],
                           manifest_by_rel: dict[str, list[int]]
                           ) -> tuple[int, int]:
    probes_total = 0
    hit_files = 0
    for rel, sink in sink_by_rel.items():
        hits, fired = _read_hits(str(root), sink,
                                 manifest_by_rel[rel])
        probes_total += fired
        hit_files += 1 if fired else 0
        clear_sink(None, str(root), sink)
    return probes_total, hit_files


def _diagnose_attribute(entry: dict, wait_commands: int,
                        timeout_s: int) -> None:
    ins = entry["instrumented"]
    base_wall = entry["baseline"].get("wall_s") or 0.0
    if ins.get("timed_out"):
        entry["attribution"] = (
            "wait_wall_dominant:instrumented capped at "
            f"{timeout_s}s (baseline {base_wall}s, waits "
            f"{wait_commands})")
    elif wait_commands and ins["wall_s"] > max(60.0, 20 * base_wall):
        entry["attribution"] = (f"slow_with_waits:{ins['wall_s']}s vs "
                                f"baseline {base_wall}s")
    else:
        entry["attribution"] = (f"other:{ins['wall_s']}s vs "
                                f"baseline {base_wall}s")


def run_canonical_diagnose(project_root: str | Path,
                           target_files: list[str], recipe: dict,
                           feature_keys: list[str], *,
                           timeout_s: int = 480,
                           progress_out: str | None = None) -> dict:
    """§SPEC-048 diagnostic-only acceptance 计时模式。

    在相同 flags 插桩（默认全 149 文件）下逐 feature 执行：同一编译套件
    先跑未插桩基线腿、再跑 flags 插桩腿，记录绝对墙钟、GUT 测试汇总、
    wait 步骤数与墙钟超限归因。不改 KOL、不排除 feature、不放宽门。
    每 feature 完成即写 progress_out（长跑分段留证）。"""
    root = Path(project_root).resolve()
    feature_by_key, sources = _diagnose_prepare(recipe, feature_keys,
                                                target_files)
    report = {
        "report_version": DIAGNOSTIC_VERSION,
        "project_root": str(root),
        "instrumentation": {"files": len(sources), "mode": "flags",
                            "recipe_hash": recipe.get("recipe_hash")},
        "features": [],
        "ok": True,
    }
    _dump_progress(report, progress_out)
    for key_spec in feature_keys:
        # "+" 组合 = 多个 feature 编译进同一套件、单进程计时（跨 feature
        # 累积成本归因；单 feature 隔离跑会漏掉进程内累积效应）
        sub_keys = [k for k in key_spec.split("+") if k]
        if not sub_keys:
            continue
        feature_text = _diagnose_feature_text(root, feature_by_key, sub_keys)
        key = key_spec
        wait_commands = len(_WAIT_STEP_RE.findall(feature_text))
        entry = {"key": key, "wait_commands": wait_commands,
                 "baseline": {}, "instrumented": {}, "attribution": None}
        suite_dir: Path | None = None
        try:
            suite_dir = create_suite_dir(root)
            compile_keys_to_suite(root, sub_keys, suite_dir)
            suite_rel = suite_dir.relative_to(root).as_posix()
            gut_args = [f"-gdir=res://{suite_rel}/generated"]
            # 腿 1：未插桩基线
            entry["baseline"] = _diagnose_baseline_leg(
                root, gut_args, timeout_s, progress_out, key)
            # 腿 2：flags 插桩（同题全量文件 + finalizer 注入）
            run_uuid = make_run_uuid()
            (backup_dir, started, sink_by_rel, manifest_by_rel,
             instrumented) = _diagnose_stage_flags(root, sources, run_uuid)
            timed_out, out, rc, wall = _diagnose_run_flags_leg(
                root, gut_args, timeout_s, backup_dir, started, instrumented)
            probes_total, hit_files = _diagnose_collect_hits(
                root, sink_by_rel, manifest_by_rel)
            if progress_out:
                Path(f"{progress_out}.{_safe_log_label(key)}.instrumented.log"
                     ).write_text(out, encoding="utf-8")
            entry["instrumented"] = {
                "wall_s": round(wall, 3), "rc": rc,
                "timed_out": timed_out, "tests": _parse_gut_summary(out),
                "hit_files": hit_files, "probes_fired_total": probes_total,
                "output_tail": out[-1500:]}
        except (SuiteError, AcceptRunError) as e:
            entry["attribution"] = f"suite_error:{e}"
        finally:
            if suite_dir is not None:
                _cleanup_suite_dir(suite_dir)
        if entry["attribution"] is None:
            _diagnose_attribute(entry, wait_commands, timeout_s)
        report["features"].append(entry)
        _dump_progress(report, progress_out)
    return report


def _performance(root: Path, probe_keys: list[str] | None,
                 timeout_s: int) -> dict:
    """W4 acceptance 成本探针（缺省 aidomain_integration）——私有目录管线
    实测「编译 + 执行」墙钟，写入 performance.acceptance_probe。"""
    from ..accept.suite import run_acceptance_probe
    keys = probe_keys if probe_keys else ["aidomain_integration"]
    try:
        probe = run_acceptance_probe(root, keys, timeout_s=timeout_s)
    except (SuiteError, AcceptRunError) as e:
        probe = {"probe_version": "acceptance-probe/1", "keys": keys,
                 "suite_ok": False, "error": str(e)}
    return {"acceptance_probe": probe}
