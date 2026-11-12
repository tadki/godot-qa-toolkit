"""§SPEC-054 canonical 静态准备缓存。

按 源码 sha256 / recipe_hash（含 toolchain 注入）/ probe 算法版本 绑定的
per-file 准备产物（func-registry、probe-manifest、插桩模板）。动态运行数据
（hits/union/bundles）永不入缓存——正确性捷径禁止。

插桩模板中的 sink 名以 uuid 占位符保存：臂间只做占位符替换（同长度 uuid），
per-file 变量名后缀来自文件 key8（与臂无关），模板跨臂、跨轮复用。

缓存条目带模板 sha256 与 registry/probe_manifest/block_map 语义内容
sha256 校验（§SPEC-060c：篡改即 quarantine + fresh 重建）；fresh 生成路径
的 reparse 守卫保留，校验和失败/版本不匹配一律回落 fresh 重建并计数缓存
未命中原因。

落点（§SPEC-052 R6 fix）：被测项目（KOL）只读零残留——持久缓存迁至
`~/.cache/gqt-canonical-prep/<root-hash12>/`（HOME 稳定命名空间），路径与
条目校验都含 project-root 身份哈希，跨项目/跨检出互不串扰。
`GQT_CANONICAL_PREP_DIR` 环境变量可整体覆写（测试隔离用）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

PREP_VERSION = "canonical-prep/2"
# probe 算法版本：插桩形态（flags/块压缩等）变化必须 bump，旧条目全失效
PROBE_ALGO_VERSION = "blocks/6"
SINK_UUID_PLACEHOLDER = "GQTUUIDSENTINEL"
# 动态数据键黑名单：缓存条目出现则视为损坏（绝不复用动态结果）
_FORBIDDEN_KEYS = ("hits", "hits_lines", "union", "unions", "bundles",
                   "covered_line_ids")


def root_id(project_root: str | os.PathLike) -> str:
    """project-root 身份（规范化绝对路径哈希）——缓存命名空间与条目校验
    双重绑定，防止跨项目/跨检出复用。"""
    norm = os.path.realpath(str(project_root))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def prep_dir(project_root: str | os.PathLike) -> "os.PathLike":
    override = os.environ.get("GQT_CANONICAL_PREP_DIR")
    base = (Path(override) if override
            else Path.home() / ".cache" / "gqt-canonical-prep")
    return str(base / root_id(project_root))


def prep_key(rel: str, src_sha256: str, recipe_hash: str,
             algo: str = PROBE_ALGO_VERSION) -> str:
    raw = f"{PREP_VERSION}|{algo}|{recipe_hash}|{rel}|{src_sha256}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _entry_path(project_root: str | os.PathLike, key: str) -> str:
    return os.path.join(str(prep_dir(project_root)), f"{key}.json")


def _entry_meta_ok(entry: dict, rid: str, rel: str, src_sha256: str,
                   recipe_hash: str, algo: str) -> bool:
    """版本/算法/身份绑定校验；任一不匹配 → False（caller 静默回落
    fresh，不 quarantine——版本演化是正常路径）。"""
    return (entry.get("version") == PREP_VERSION
            and entry.get("algo") == algo
            and entry.get("root_id") == rid
            and entry.get("rel") == rel
            and entry.get("src_sha256") == src_sha256
            and entry.get("recipe_hash") == recipe_hash)


def _has_forbidden_keys(entry: dict) -> bool:
    """动态数据键黑名单：命中即视为损坏（绝不复用动态结果）。"""
    return any(k in entry for k in _FORBIDDEN_KEYS)


def _probe_lines_ok(entry: dict) -> list | None:
    """probe_lines 形状校验：非 int 列表 → None（损坏）。"""
    probe_lines = entry.get("probe_lines")
    if not isinstance(probe_lines, list) or not all(
            isinstance(x, int) for x in probe_lines):
        return None
    return probe_lines


def _sha256_obj(obj) -> str:
    """语义内容完整性指纹：键统一 str + 显式排序后 canonical json——
    JSON 往返把 int 键变 str，指纹必须对往返稳定形态计算。"""
    def canon(o):
        if isinstance(o, dict):
            return {str(k): canon(v) for k, v in sorted(
                o.items(), key=lambda kv: str(kv[0]))}
        if isinstance(o, (list, tuple)):
            return [canon(x) for x in o]
        return o

    return hashlib.sha256(json.dumps(
        canon(obj), ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _normalize_block_map(block_map) -> dict:
    """块映射键统一 str（存储形态 = JSON 往返稳定形态）。非 int 键/值 →
    ValueError（caller 按损坏处理）。"""
    try:
        return {str(int(k)): sorted(int(x) for x in v)
                for k, v in dict(block_map or {}).items()}
    except (TypeError, ValueError) as e:
        raise ValueError(f"block_map malformed: {e}") from e


def _denormalize_block_map(block_map) -> dict:
    """load 侧还原 int 键（orchestrator 以 int 起始行查块映射）。"""
    try:
        return {int(k): list(v) for k, v in dict(block_map or {}).items()}
    except (TypeError, ValueError) as e:
        raise ValueError(f"block_map malformed: {e}") from e


def _semantic_integrity_ok(entry: dict) -> bool:
    """§SPEC-060c：registry/probe_manifest/block_map 语义内容 sha256 校验
    （§SPEC-053 cc_oracle 连接与探针清单直接消费这些内容，被篡改的缓存
    会把伪造 CC 当真）。缺失/类型错/指纹不匹配 → False（quarantine）。"""
    registry = entry.get("registry")
    manifest = entry.get("probe_manifest")
    block_map = entry.get("block_map")
    if not isinstance(registry, dict) or not isinstance(manifest, dict) \
            or not isinstance(block_map, dict):
        return False
    try:
        bm_norm = _normalize_block_map(block_map)
    except ValueError:
        return False
    return (entry.get("registry_sha256") == _sha256_obj(registry)
            and entry.get("probe_manifest_sha256") == _sha256_obj(manifest)
            and entry.get("block_map_sha256") == _sha256_obj(bm_norm))


def _template_ok(entry: dict, tpl: str, probe_lines: list) -> bool:
    """模板完整性：sha256 校验 + vacuous/占位符形态。"""
    if hashlib.sha256(tpl.encode("utf-8")).hexdigest() != entry.get(
            "template_sha256"):
        return False
    if not probe_lines:
        # 零可执行行：模板 = 原文，无占位符（vacuous 车道，合法形态）
        return tpl == entry.get("source_plain")
    return SINK_UUID_PLACEHOLDER in tpl


def load_entry(project_root: str | os.PathLike, *, rel: str,
               src_sha256: str, recipe_hash: str,
               algo: str = PROBE_ALGO_VERSION) -> dict | None:
    """命中返回 {registry, probe_manifest, instrumented_template}；任何不
    匹配/损坏/含动态键 → None（caller 重建）。保证不抛异常吞错：损坏文件
    显式落盘 .bad 后缀供排查。"""
    rid = root_id(project_root)
    path = _entry_path(project_root, prep_key(rel, src_sha256, recipe_hash,
                                              algo))
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            entry = json.loads(f.read())
    except (OSError, ValueError):
        _quarantine(path)
        return None
    if not _entry_meta_ok(entry, rid, rel, src_sha256, recipe_hash, algo):
        return None
    if _has_forbidden_keys(entry):
        _quarantine(path)
        return None
    tpl = entry.get("instrumented_template")
    if not isinstance(tpl, str):
        _quarantine(path)
        return None
    probe_lines = _probe_lines_ok(entry)
    if probe_lines is None:
        _quarantine(path)
        return None
    if not _semantic_integrity_ok(entry):
        _quarantine(path)
        return None
    if not _template_ok(entry, tpl, probe_lines):
        _quarantine(path)
        return None
    try:
        block_map = _denormalize_block_map(entry.get("block_map") or {})
    except ValueError:
        _quarantine(path)
        return None
    return {"registry": entry["registry"],
            "probe_manifest": entry["probe_manifest"],
            "instrumented_template": tpl,
            "probe_lines": probe_lines,
            "block_map": block_map}


def store_entry(project_root: str | os.PathLike, *, rel: str,
                src_sha256: str, recipe_hash: str, registry: dict,
                probe_manifest: dict, instrumented_template: str,
                probe_lines: list[int], source_plain: str = "",
                block_map: dict | None = None,
                algo: str = PROBE_ALGO_VERSION) -> str:
    """原子写入（tmp+rename）。仅静态产物——动态数据禁止入内。"""
    rid = root_id(project_root)
    import tempfile

    try:
        block_map = _normalize_block_map(block_map)
    except ValueError as e:
        # 缓存写失败不阻断测量（orchestrator 按 OSError 隔离处理）
        raise OSError(f"cache store rejected malformed block_map: {e}") from e
    root = prep_dir(project_root)
    os.makedirs(root, exist_ok=True)
    key = prep_key(rel, src_sha256, recipe_hash, algo)
    path = _entry_path(project_root, key)
    entry = {
        "version": PREP_VERSION,
        "algo": algo,
        "root_id": rid,
        "rel": rel,
        "src_sha256": src_sha256,
        "recipe_hash": recipe_hash,
        "template_sha256": hashlib.sha256(
            instrumented_template.encode("utf-8")).hexdigest(),
        "registry_sha256": _sha256_obj(registry),
        "probe_manifest_sha256": _sha256_obj(probe_manifest),
        "block_map_sha256": _sha256_obj(block_map),
        "registry": registry,
        "probe_manifest": probe_manifest,
        "instrumented_template": instrumented_template,
        "probe_lines": list(probe_lines),
        "source_plain": source_plain,
        "block_map": block_map,
    }
    fd, tmp = tempfile.mkstemp(dir=root, prefix=f".{key}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _quarantine(path: str) -> None:
    try:
        os.replace(path, path + ".bad")
    except OSError:
        pass
