"""SEE-1615 §SPEC-ARGMERGE —— --attribution-map 三态合并 + 兼容别名。

三态 `auto|off|<path>`：auto（默认）= env 语义不变；off = 关闭自动
（ENV_MODE=off，无可用映射 fail-loud）；<path> = 显式产物只读透传
（ENV_MAP=path）。`--no-attribution-auto` 保留为 off 的兼容别名。
env GQT_ATTRIBUTION_MAP_MODE 语义不变，CLI 显式值覆盖 env。
"""

from __future__ import annotations

import argparse

from godot_qa_toolkit.cli import _apply_mutation_map_flags
from godot_qa_toolkit.mutation import truth_select as ts


def _args(**kw) -> argparse.Namespace:
    kw.setdefault("attribution_map", None)
    kw.setdefault("attribution_auto", True)
    return argparse.Namespace(**kw)


def test_default_auto_leaves_env_untouched(monkeypatch):
    monkeypatch.delenv(ts.ENV_MAP, raising=False)
    monkeypatch.delenv(ts.ENV_MODE, raising=False)
    _apply_mutation_map_flags(_args(), ts)
    assert ts.ENV_MAP not in __import__("os").environ
    assert ts.ENV_MODE not in __import__("os").environ


def test_three_state_auto_clears_env(monkeypatch):
    monkeypatch.setenv(ts.ENV_MODE, "off")
    _apply_mutation_map_flags(_args(attribution_map="auto"), ts)
    assert ts.ENV_MODE not in __import__("os").environ
    assert ts.ENV_MAP not in __import__("os").environ


def test_three_state_off_disables(monkeypatch):
    monkeypatch.delenv(ts.ENV_MAP, raising=False)
    monkeypatch.delenv(ts.ENV_MODE, raising=False)
    _apply_mutation_map_flags(_args(attribution_map="off"), ts)
    import os
    assert os.environ[ts.ENV_MODE] == "off"
    assert ts.ENV_MAP not in os.environ


def test_three_state_path_sets_explicit(monkeypatch):
    monkeypatch.delenv(ts.ENV_MAP, raising=False)
    monkeypatch.setenv(ts.ENV_MODE, "off")
    _apply_mutation_map_flags(
        _args(attribution_map="/tmp/explicit-map.json"), ts)
    import os
    assert os.environ[ts.ENV_MAP] == "/tmp/explicit-map.json"
    assert ts.ENV_MODE not in os.environ  # 显式产物不受 off 模式影响


def test_legacy_no_auto_alias_equals_off(monkeypatch):
    monkeypatch.delenv(ts.ENV_MAP, raising=False)
    monkeypatch.delenv(ts.ENV_MODE, raising=False)
    _apply_mutation_map_flags(_args(attribution_auto=False), ts)
    import os
    assert os.environ[ts.ENV_MODE] == "off"
    assert ts.ENV_MAP not in os.environ


def test_legacy_alias_overridden_by_three_state(monkeypatch):
    monkeypatch.delenv(ts.ENV_MAP, raising=False)
    monkeypatch.delenv(ts.ENV_MODE, raising=False)
    _apply_mutation_map_flags(
        _args(attribution_map="auto", attribution_auto=False), ts)
    # 三态显式值在后处理——auto 清掉 alias 的 off（按 CLI 顺序
    # --attribution-map 为新主旋钮）
    import os
    assert ts.ENV_MODE not in os.environ
    assert ts.ENV_MAP not in os.environ


def test_env_semantics_unchanged(monkeypatch):
    """env GQT_ATTRIBUTION_MAP_MODE 语义不变：CLI 未显式时 env 保持权威。"""
    monkeypatch.setenv(ts.ENV_MODE, "auto")
    _apply_mutation_map_flags(_args(), ts)
    import os
    assert os.environ[ts.ENV_MODE] == "auto"
