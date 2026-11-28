"""C3-R15：gdtoolkit 语法缓存目录并发预热。

gdtoolkit 的 `parser._get_parser` 用裸 `os.makedirs(cache_dirpath)`（无
exist_ok）建 `<HOME>/.cache/gdtoolkit/<version>`——同一 HOME 下两个 gqt
进程并发首次解析时一方 `FileExistsError` 崩溃（QA 登记的并发建图一成
一败）。gqt 在首次使用 gdtoolkit 解析前以 `exist_ok=True` 预热该目录，
库内 makedirs 因此命中"已存在"分支被跳过，竞争窗口消除（每进程一次）。"""

from __future__ import annotations

import os
from pathlib import Path

_WARMED = False

_PARSER_WRAPPED = False


def _wrap_parser_cache_makedirs(parser_cls, cache_dirpath: str) -> None:
    """C3-R16：兜住**任何路径**的第一次 `parse()`——库内
    `_get_parser` 用裸 makedirs（无 exist_ok），预热只挂调用点会漏
    （如 coverage/instrument 的 `_collect_executable_lines` 早于全部
    现有挂点）。进程级一次性 wrap：进入前 exist_ok 预热，幂等。"""
    global _PARSER_WRAPPED
    if _PARSER_WRAPPED:
        return
    _PARSER_WRAPPED = True
    original = parser_cls._get_parser

    def _get_parser_with_warm_cache(self, name, *args, **kwargs):
        try:
            os.makedirs(cache_dirpath, exist_ok=True)
        except OSError:
            pass
        return original(self, name, *args, **kwargs)

    parser_cls._get_parser = _get_parser_with_warm_cache


def ensure_parser_cache_dir(force_home: "str | None" = None) -> None:
    """幂等预热 gdtoolkit 语法缓存目录（每进程一次，失败静默——预热
    只是消除库内裸 makedirs 的并发竞争；解析本身不依赖本预热成功）。"""
    global _WARMED
    if _WARMED and not force_home:
        return
    try:
        from importlib.metadata import version as _pkg_version

        from gdtoolkit.parser.parser import get_cache_directory, Parser
        version = _pkg_version("gdtoolkit")
        if force_home:
            cache_dirpath = os.path.join(
                str(Path(force_home) / ".cache" / "gdtoolkit"), version)
        else:
            cache_dirpath = os.path.join(get_cache_directory(), "gdtoolkit",
                                         version)
        os.makedirs(cache_dirpath, exist_ok=True)
        _wrap_parser_cache_makedirs(Parser, cache_dirpath)
        _WARMED = True
    except Exception:  # noqa: BLE001 — gdtoolkit 缺席/版本探测失败等
        return
