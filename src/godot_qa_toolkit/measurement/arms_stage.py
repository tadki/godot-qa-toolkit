"""SEE-1594 BTIDY.1：measurement 臂舞台支撑——模板装载、分片计划、created
清单、根树注入与还原。自 arms.py 物理迁移（零行为）；arms 侧以导入绑定
保持既有补丁面（arms._arm_inject_root / arms._arm_restore 等）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .backup import MeasurementError, batch_sink_name
from .probe_templates import (
    _ATTR_CTX_RELPATH,
    _FLUSHER_RELPATH,
    _PRE_HOOK_RELPATH,
)
from .shadowio import _shard_groups


def _stage_arm_files(name: str, prepared: dict, run_uuid: str,
                     test_attribution: bool) -> tuple:
    """臂内模板→sink/manifest/插桩文本装载（方向②③：归属只在 gut_full
    臂启用）。返回 6 元组（started/sinks/manifests/instrumented/inst/vacuous）。"""
    started = {"run_uuid": run_uuid, "project_root": "",
               "files": {}}
    sink_by_rel: dict[str, str] = {}
    manifest_by_rel: dict[str, list[int]] = {}
    instrumented_files: dict[str, str] = {}
    inst_by_rel: dict[str, str] = {}
    from . import prepcache

    vacuous_rels: list[str] = []
    for rel, item in prepared.items():
        use_attr = test_attribution and name == "gut_full"
        template = item.get("template_attr") if use_attr else item.get("template")
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
            raise MeasurementError(
                f"sink placeholder missing for {rel} (stale cache?)")
        started["files"][item["abs"]] = item["src"]
        sink_by_rel[rel] = sink
        manifest_by_rel[rel] = manifest
        instrumented_files[item["abs"]] = inst
        inst_by_rel[rel] = inst
    return (started, sink_by_rel, manifest_by_rel, instrumented_files,
            inst_by_rel, vacuous_rels)

def _count_test_scripts(root: Path, dirs: list[str]) -> int:
    """SEE-1623 M10：分片目录组内真实收集的 `test_*.gd` 脚本数。

    与 `shadowio._shard_groups` 的加权发现同源（同一 rglob 口径）——
    该计数作为 gut_full 臂的"收集执行面"证据随分片计划落盘，
    支撑 `empty_test_face` / `no_execution_evidence` 具名门。"""
    n = 0
    for d in dirs:
        rel = d[len("res://"):] if str(d).startswith("res://") else str(d)
        p = root / rel
        if p.is_dir():
            n += len(list(p.rglob("test_*.gd")))
    return n


def _arm_plans(root: Path, recipe: dict, gherkin: dict | None,
               shard_width: int) -> list[dict]:
    """分片计划：gherkin 组由调用方编译；gut_full 按 gutconfig dirs。"""
    if gherkin is not None:
        return [
            {"args": ["-gdir=res://" + Path(g["suite_dir"]).relative_to(
                root).as_posix() + "/generated"],
             "keys": g["keys"], "suite_dir": Path(g["suite_dir"])}
            for g in gherkin["groups"]]
    groups = _shard_groups(
        root, list(recipe["gut_face"].get("dirs") or []),
        shard_width)
    return [{"args": ([("-gdir=" + ",".join(g))] if g else []),
             "keys": None, "suite_dir": None,
             "collected": _count_test_scripts(root, g)} for g in groups]

def _arm_stage_created(root: Path, started: dict, name: str,
                       attribution_causal: bool, shadow_instrument: bool,
                       backup_dir: Path) -> None:
    """project.godot 存在性检查 + created 清单 + 非 shadow 臂落 started.json。

    §SPEC-062A：project.godot 全程只读——finalizer 经 GUT pre_run_script
    通道注册，不再改写 [autoload] 表。"""
    pg_path = root / "project.godot"
    if not pg_path.is_file():
        raise MeasurementError(
            "cannot run measurement: project.godot missing")
    started["created"] = [str(root / _FLUSHER_RELPATH),
                          str(root / _PRE_HOOK_RELPATH)]
    if attribution_causal and name == "gut_full":
        started["created"].append(str(root / _ATTR_CTX_RELPATH))
    if not shadow_instrument:
        (backup_dir / "started.json").write_text(
            json.dumps(started, ensure_ascii=False), encoding="utf-8")

def _arm_inject_root(root: Path, instrumented_files: dict[str, str],
                     shadow_hook_files: list[tuple[str, str]],
                     attribution_causal: bool, name: str) -> None:
    """非 shadow 臂：根树写入插桩文本与钩子；attr ctx 脚本生成期先
    parse 一次（注入脚本解析失败会让全部被插桩文件在 load 期失效——
    探针静默零命中），fail-loud 拒绝跑假数据。"""
    for abs_path, inst in instrumented_files.items():
        Path(abs_path).write_text(inst, encoding="utf-8")
    # 方向②③：attr 钩子只在归属臂注入；gherkin 臂用默认钩子
    if attribution_causal and name == "gut_full":
        from gdtoolkit.parser import parser as _ctx_parser
        from .probe_templates import _ATTR_CTX_GD
        try:
            _ctx_parser.parse(_ATTR_CTX_GD)
        except Exception as e:  # noqa: BLE001
            raise MeasurementError(
                f"causal context script failed to parse: "
                f"{e}") from e
    for relpath, text in shadow_hook_files:
        abs_p = root / relpath
        abs_p.parent.mkdir(parents=True, exist_ok=True)
        abs_p.write_text(text, encoding="utf-8")

def _arm_restore(root: Path, started: dict, backup_dir: Path) -> None:
    """finally 还原：插桩文件写回原文、created 清单删除、备份目录删除。"""
    for abs_path, original in started["files"].items():
        Path(abs_path).write_text(original, encoding="utf-8")
    for created in started.get("created", []):
        Path(created).unlink(missing_ok=True)
    shutil.rmtree(backup_dir, ignore_errors=True)
