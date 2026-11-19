"""SEE-1594 §SPEC-012：形状门扫描器最小反例（pytest 侧驱动钉行为）。

扫描器本体 = tests/integration/check_see1594_shape.py（此前仅集成侧
手工跑、无 pytest 判据——hardener S1 注入「文件尺寸谓词取反」存活
暴露的缺口：干净树必须 exit 0；超限文件/超限函数各自违规。）
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

_SCANNER = (Path(__file__).resolve().parents[1] / "integration"
            / "check_see1594_shape.py")


def _load_scanner():
    spec = importlib.util.spec_from_file_location("see1594_shape", _SCANNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "godot_qa_toolkit" / "pkg").mkdir(parents=True)
    return root


def _run(mod, tree: Path, out: Path, *, max_file: int = 20,
         max_func: int = 10) -> tuple[int, dict]:
    mod.REPO_ROOT = tree
    mod.SRC_ROOT = tree / "src" / "godot_qa_toolkit"
    rc = mod.main(["--areas", "pkg", "--max-file-lines", str(max_file),
                   "--max-function-lines", str(max_func),
                   "--out", str(out)])
    return rc, json.loads(out.read_text(encoding="utf-8"))


class TestShapeGate:
    def test_clean_tree_ok(self, tmp_path):
        """达标树 → exit 0、零违规（门谓词方向的负反例）。"""
        mod = _load_scanner()
        tree = _tree(tmp_path)
        pkg = tree / "src" / "godot_qa_toolkit" / "pkg"
        (pkg / "small.py").write_text(
            "def f():\n"
            + "".join(f"    x{i} = {i}\n" for i in range(5))
            + "    return x0\n")
        rc, doc = _run(mod, tree, tmp_path / "shape.json")
        assert rc == 0, doc["violations"]
        assert doc["ok"] is True and doc["violations"] == []

    def test_oversized_file_flagged(self, tmp_path):
        """文件行数超限 → 违规、exit 1、具名位置。"""
        mod = _load_scanner()
        tree = _tree(tmp_path)
        pkg = tree / "src" / "godot_qa_toolkit" / "pkg"
        (pkg / "big.py").write_text(
            "".join(f"# line {i}\n" for i in range(21)))
        rc, doc = _run(mod, tree, tmp_path / "shape.json")
        assert rc == 1
        assert any(v.get("kind") == "file_lines"
                   and v.get("path", "").endswith("big.py")
                   for v in doc["violations"]), doc["violations"]

    def test_oversized_function_flagged(self, tmp_path):
        """文件行数达标但函数超限 → 违规（函数维度独立判定）。"""
        mod = _load_scanner()
        tree = _tree(tmp_path)
        pkg = tree / "src" / "godot_qa_toolkit" / "pkg"
        body = "".join(f"    x{i} = {i}\n" for i in range(11))
        text = f"def g():\n{body}    return x0\n"  # 13 行 ≤ 20；g 为 13 行 > 10
        (pkg / "longfunc.py").write_text(text)
        rc, doc = _run(mod, tree, tmp_path / "shape.json",
                       max_file=20, max_func=10)
        assert rc == 1
        assert any(v.get("kind") == "function_lines"
                   and v.get("path", "").endswith("longfunc.py")
                   and v.get("name") == "g"
                   for v in doc["violations"]), doc["violations"]

    def test_exact_limit_not_flagged(self, tmp_path):
        """边界：恰在限值上（20 行文件 / 10 行函数）不算超限（> 语义）；
        超 1 行的邻居文件照常违规（同轮对照）。"""
        mod = _load_scanner()
        tree = _tree(tmp_path)
        pkg = tree / "src" / "godot_qa_toolkit" / "pkg"
        body = "".join(f"    x{i} = {i}\n" for i in range(8))
        text = f"def h():\n{body}    return x0\n"  # 10 行文件 / 10 行函数
        (pkg / "edge.py").write_text(text)
        (pkg / "edge2.py").write_text(text + "# pad\n")  # 11 行文件 ≤ 20
        (pkg / "edge3.py").write_text(
            text + "".join(f"# pad {i}\n" for i in range(11)))  # 21 行 > 20
        rc, doc = _run(mod, tree, tmp_path / "shape.json",
                       max_file=20, max_func=10)
        assert rc == 1
        assert any(v.get("path", "").endswith("edge3.py")
                   for v in doc["violations"]), doc["violations"]
        assert not any(v.get("path", "").endswith(("edge.py", "edge2.py"))
                       for v in doc["violations"]), doc["violations"]
