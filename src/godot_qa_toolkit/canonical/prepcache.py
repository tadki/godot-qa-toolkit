"""§SPEC-054 canonical 静态准备缓存。

按 源码 sha256 / recipe_hash（含 toolchain 注入）/ probe 算法版本 绑定的
per-file 准备产物（func-registry、probe-manifest、插桩模板）。动态运行数据
（hits/union/bundles）永不入缓存——正确性捷径禁止。

插桩模板中的 sink 名以 uuid 占位符保存：臂间只做占位符替换（同长度 uuid），
per-file 变量名后缀来自文件 key8（与臂无关），模板跨臂、跨轮复用。

缓存条目带模板 sha256 校验（防损坏）；fresh 生成路径的 reparse 守卫保留，
校验和失败/版本不匹配一律回落 fresh 重建并计数缓存未命中原因。
"""

from __future__ import annotations

import hashlib
import json
import os

PREP_VERSION = "canonical-prep/1"
# probe 算法版本：插桩形态（flags/块压缩等）变化必须 bump，旧条目全失效
PROBE_ALGO_VERSION = "blocks/6"
SINK_UUID_PLACEHOLDER = "GQTUUIDSENTINEL"
# 动态数据键黑名单：缓存条目出现则视为损坏（绝不复用动态结果）
_FORBIDDEN_KEYS = ("hits", "hits_lines", "union", "unions", "bundles",
                   "covered_line_ids")


def prep_dir(project_root: str | os.PathLike) -> "os.PathLike":
    return os.path.join(str(project_root), ".qa-cache",
                        "gqt-canonical-prep")


def prep_key(rel: str, src_sha256: str, recipe_hash: str,
             algo: str = PROBE_ALGO_VERSION) -> str:
    raw = f"{PREP_VERSION}|{algo}|{recipe_hash}|{rel}|{src_sha256}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _entry_path(project_root, key: str) -> str:
    return os.path.join(str(prep_dir(project_root)), f"{key}.json")


def load_entry(project_root, *, rel: str, src_sha256: str,
               recipe_hash: str, algo: str = PROBE_ALGO_VERSION) -> dict | None:
    """命中返回 {registry, probe_manifest, instrumented_template}；任何不
    匹配/损坏/含动态键 → None（caller 重建）。保证不抛异常吞错：损坏文件
    显式落盘 .bad 后缀供排查。"""
    path = _entry_path(project_root, prep_key(rel, src_sha256, recipe_hash,
                                              algo))
    if not os.path.isfile(path):
        return None
    try:
        entry = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        _quarantine(path)
        return None
    if (entry.get("version") != PREP_VERSION
            or entry.get("algo") != algo
            or entry.get("rel") != rel
            or entry.get("src_sha256") != src_sha256
            or entry.get("recipe_hash") != recipe_hash):
        return None
    for k in _FORBIDDEN_KEYS:
        if k in entry:
            _quarantine(path)
            return None
    tpl = entry.get("instrumented_template")
    if not isinstance(tpl, str):
        _quarantine(path)
        return None
    if hashlib.sha256(tpl.encode("utf-8")).hexdigest() != entry.get(
            "template_sha256"):
        _quarantine(path)
        return None
    # vacuous（无探针）条目模板=原文、无占位符：仅要求模板与原文一致
    probe_lines = entry.get("probe_lines")
    if (not isinstance(probe_lines, list)
            or not all(isinstance(x, int) for x in probe_lines)):
        _quarantine(path)
        return None
    block_map = entry.get("block_map") or {}
    if not probe_lines:
        # 零可执行行：模板 = 原文，无占位符（vacuous 车道，合法形态）
        if tpl != entry.get("source_plain"):
            _quarantine(path)
            return None
    elif SINK_UUID_PLACEHOLDER not in tpl:
        _quarantine(path)
        return None
    return {"registry": entry["registry"],
            "probe_manifest": entry["probe_manifest"],
            "instrumented_template": tpl,
            "probe_lines": probe_lines,
            "block_map": block_map}


def store_entry(project_root, *, rel: str, src_sha256: str,
                recipe_hash: str, registry: dict, probe_manifest: dict,
                instrumented_template: str, probe_lines: list[int],
                source_plain: str = "", block_map: dict | None = None,
                algo: str = PROBE_ALGO_VERSION) -> str:
    """原子写入（tmp+rename）。仅静态产物——动态数据禁止入内。"""
    import tempfile

    root = prep_dir(project_root)
    os.makedirs(root, exist_ok=True)
    key = prep_key(rel, src_sha256, recipe_hash, algo)
    path = _entry_path(project_root, key)
    entry = {
        "version": PREP_VERSION,
        "algo": algo,
        "rel": rel,
        "src_sha256": src_sha256,
        "recipe_hash": recipe_hash,
        "template_sha256": hashlib.sha256(
            instrumented_template.encode("utf-8")).hexdigest(),
        "registry": registry,
        "probe_manifest": probe_manifest,
        "instrumented_template": instrumented_template,
        "probe_lines": list(probe_lines),
        "source_plain": source_plain,
        "block_map": block_map or {},
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
