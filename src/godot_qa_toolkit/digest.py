"""SEE-1594 BCODE §SPEC-011：内容摘要单一 helper（排序稳定 JSON / 文本 /
文件 sha256）。

此前同一语义散落 ≥3 处（coverlink._canonical_sha256 / recipe recipe_hash
basis / tests_face_digest，另有文件 sha256 两处实现）——统一为本模块，
序列化口径（ensure_ascii=False、sort_keys、紧凑分隔符）即为跨模块可比
指纹的唯一出典。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

_CHUNK = 1024 * 1024


def canonical_json(obj) -> str:
    """排序稳定紧凑 JSON（摘要序列化的唯一口径）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha256(obj) -> str:
    """任意 JSON 值的排序稳定 sha256（排序稳定 JSON + sha256_text）。"""
    return sha256_text(canonical_json(obj))


def sha256_file(path: str | Path) -> str:
    """文件内容 sha256（分块读取，大文件不整读入内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
