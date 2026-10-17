"""SEE-1426 Round A hardener — `gqt arch` 对抗性单测（§SPEC-1426-15 arch 面）。

对抗预设：默认守卫会放行作弊。每个测试对应一种已实证的绕过/误报形态：
- 绕过尝试：inner class extends Node、字符串路径 extends、成员形态伪装的
  裸随机、变量注解形态的引擎引用——各必打回（探针实证当前可打回）。
- 误报面（判别力另一半）：注释/字符串字面量里的禁词不得错杀合法 Pure Core。
- 注入缺口正例：RNG 参数类型注解注入、无 extends 默认 RefCounted，必须放行。

发现披露（见 hardener 汇报，不修复）：`seed()` 全局种子函数不在
BARE_GLOBAL_RANDOM 名单（规格枚举未含，规则 v1 范围决策，建议 Round B
按开放枚举加法演进）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from godot_qa_toolkit.arch.collector import collect_file
from godot_qa_toolkit.arch.gate import run_arch_gate


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------- 绕过尝试（必打回） ----------

def test_inner_class_extends_node_rejected(tmp_path: Path) -> None:
    # 绕过形态：顶层 extends 合法，把 Node 系继承藏进 inner class。
    p = _write(tmp_path, "core/tricky.gd",
               "extends RefCounted\nclass Inner:\n\textends Node\n")
    violations, err = collect_file(p)
    assert err is None
    assert violations, "inner class extends Node must not slip through"
    assert {v.rule for v in violations} & {
        "core/extends-whitelist", "no-engine-api"}


def test_string_path_extends_rejected(tmp_path: Path) -> None:
    # 绕过形态：extends "res://x.gd" 以字符串路径引用任意（可能 Node 系）
    # 基类——白名单无法核验路径指向，必须保守打回。
    p = _write(tmp_path, "core/bypath.gd",
               'extends "res://scenes/thing.gd"\n')
    violations, _ = collect_file(p)
    assert any(v.rule == "core/extends-whitelist" for v in violations)


def test_member_form_random_without_rng_decl_rejected(tmp_path: Path) -> None:
    # 伪装形态：裸随机写成成员调用 self.randi()，文件里并无 RNG 注入
    # 声明——成员形式不得成为无注入时的逃逸通道。
    p = _write(tmp_path, "core/selfrand.gd",
               "extends RefCounted\nfunc f() -> int:\n\treturn self.randi()\n")
    violations, _ = collect_file(p)
    assert any(v.rule == "no-bare-global-random" for v in violations)


def test_variable_type_annotation_engine_rejected(tmp_path: Path) -> None:
    # 引擎引用藏在成员变量类型注解（既有套件只覆盖函数签名注解形态）。
    p = _write(tmp_path, "core/varnode.gd",
               "extends RefCounted\nvar n: Node2D = null\n")
    violations, _ = collect_file(p)
    assert any(v.rule == "no-engine-api" and "Node2D" in v.message
               for v in violations)


@pytest.mark.parametrize("call", [
    "randi", "randf", "randfn", "randi_range", "randf_range", "randomize",
])
def test_bare_global_random_family_matrix(tmp_path: Path, call: str) -> None:
    # 裸全局随机六函数全家族逐一打回（既有套件只抽测 randi/randf）。
    p = _write(tmp_path, "core/fam.gd",
               f"extends RefCounted\nfunc f() -> void:\n\t{call}()\n")
    violations, _ = collect_file(p)
    assert any(v.rule == "no-bare-global-random" for v in violations), call


# ---------- 误报面（守卫不得错杀合法代码） ----------

def test_comment_mentions_not_flagged(tmp_path: Path) -> None:
    p = _write(tmp_path, "core/comment.gd",
               "extends RefCounted\n"
               "# randi() get_tree() Node Engine 全在注释里\n"
               "func f() -> int:\n\treturn 1\n")
    violations, err = collect_file(p)
    assert err is None
    assert violations == []


def test_string_literal_mentions_not_flagged(tmp_path: Path) -> None:
    p = _write(tmp_path, "core/str.gd",
               'extends RefCounted\nfunc f() -> String:\n'
               '\treturn "get_tree() randi() Node"\n')
    violations, err = collect_file(p)
    assert err is None
    assert violations == []


# ---------- 注入缺口正例（防过严） ----------

def test_rng_parameter_annotation_passes(tmp_path: Path) -> None:
    # 显式 RNG 注入的合法形态：参数类型注解 RandomNumberGenerator。
    p = _write(tmp_path, "core/rngparam.gd",
               "extends RefCounted\n"
               "func roll(rng: RandomNumberGenerator) -> int:\n"
               "\treturn rng.randi_range(1, 6)\n")
    violations, err = collect_file(p)
    assert err is None
    assert violations == []


def test_no_extends_defaults_refcounted_passes(tmp_path: Path) -> None:
    # GDScript 省略 extends 默认 RefCounted——纯逻辑文件合法，不得误报。
    p = _write(tmp_path, "core/plain.gd",
               "var hp: int = 3\nfunc hit(n: int) -> int:\n\treturn hp - n\n")
    violations, err = collect_file(p)
    assert err is None
    assert violations == []


# ---------- gate 契约边界 ----------

def test_gate_empty_directory_is_ok(tmp_path: Path) -> None:
    # 空输入边界：零文件不 crash，无违规可报即 ok（unparseable 同理为 0）。
    d = tmp_path / "empty"
    d.mkdir()
    result = run_arch_gate([str(d)])
    assert result["ok"] is True
    assert result["failures"] == []
    assert result["summary"]["unparseable"] == 0


def test_gate_by_rule_aggregation(tmp_path: Path) -> None:
    _write(tmp_path, "m/a.gd", "extends Node\n")
    _write(tmp_path, "m/b.gd",
           "extends RefCounted\nfunc f() -> void:\n\tget_tree()\n\trandi()\n")
    result = run_arch_gate([str(tmp_path)])
    assert result["ok"] is False
    # extends Node 双重命中：extends-whitelist（基类越界）+ no-engine-api
    # （Node 裸标识符）——规则独立评估，fail-closed 语义。
    assert result["summary"]["by_rule"]["core/extends-whitelist"] == 1
    assert result["summary"]["by_rule"]["no-engine-api"] == 2
    assert result["summary"]["by_rule"]["no-bare-global-random"] == 1
    assert result["summary"]["files"] == 2
    assert result["summary"]["violations"] == 4
