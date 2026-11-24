"""SEE-1624 严格单根 addon 布局的共享测试夹具工厂。

单根契约（§SPEC-002/003）下 feature 只能来自
`<root>/addons/gqt_gherkin/features/`，且 compile/run 前都要过 schema
握手——本工厂是全部 gherkin 测试构造合法布局的唯一入口，禁止各测试
私搭旧布局（acceptance/features、addons/gqt_accept 一律不再是输入）。
"""

from __future__ import annotations

import json
from pathlib import Path

FEATURE = """Feature: 生命值扣减
  Scenario Outline: 受到 <dmg> 点伤害
    Given hero hp is <hp>
    When hero takes <dmg> damage
    Then hero hp is <left>

    # types: <hp>:int, <dmg>:int, <left>:numeric
    Examples:
      | hp | dmg | left |
      | 10 | 3   | 7    |
      | 20 | 20  | 0    |
"""

SECOND_FEATURE = """Feature: 纯场景
  Scenario: 简单通过
    Given world exists
"""

# R2 批准字段（§SPEC-019 终稿）：schema（<major>[.<minor>]）/ requires_gqt /
# testing_api；未知键忽略（minor=加法）。
VALID_SCHEMA: dict = {
    "schema": "gqt-gherkin-addon/1.0",
    "requires_gqt": "v0.4.2",
    "testing_api": "see1468-gherkin/2",
}

# 真实可被 Godot `-s` 拉起的 validator 最小实现：V 检查 = schema 可解析、
# harness 在场、features 非空；verdict 单行 JSON（supported_majors 与
# GQT 支持集等值由 GQT 侧断言）。
VALIDATOR_OK_GD = """# test validator stub (real SceneTree script)
extends SceneTree

func _init() -> void:
	var ok := true
	var reasons: Array = []
	var f := FileAccess.open("res://addons/gqt_gherkin/schema.json", FileAccess.READ)
	if f == null:
		ok = false
		reasons.append("schema.json unreadable")
	else:
		if JSON.parse_string(f.get_as_text()) == null:
			ok = false
			reasons.append("schema.json unparseable")
	if not ResourceLoader.exists("res://addons/gqt_gherkin/harness.gd"):
		ok = false
		reasons.append("harness.gd missing")
	var feats := DirAccess.get_files_at("res://addons/gqt_gherkin/features")
	if feats.is_empty():
		ok = false
		reasons.append("features empty")
	print(JSON.stringify({"ok": ok, "supported_majors": [1],
		"reason": "" if ok else "; ".join(reasons)}))
	quit(0)
"""


def make_addon_project(root: Path, *,
                       features: dict[str, str] | None = None,
                       schema: dict | str | None = None,
                       validator_gd: str | None = VALIDATOR_OK_GD,
                       harness: bool = True,
                       gut: bool = True,
                       gutconfig: bool = True) -> Path:
    """构造合法（或按参数破坏的）单根 addon 项目布局。features 形如
    {"<stem>": "<feature text>"}（None = 默认 demo；{} = 空集负例）；
    schema 传 dict 或原始字符串覆盖默认；validator_gd=None 移除
    validator.gd（缺 validator 负例）。"""
    root.mkdir(parents=True, exist_ok=True)
    addon = root / "addons" / "gqt_gherkin"
    (addon / "features").mkdir(parents=True, exist_ok=True)
    schema_doc = schema if schema is not None else VALID_SCHEMA
    if isinstance(schema_doc, dict):
        schema_doc = json.dumps(schema_doc, ensure_ascii=False, indent=2)
    (addon / "schema.json").write_text(schema_doc, encoding="utf-8")
    if harness:
        (addon / "harness.gd").write_text(
            "# harness stub — testing_api adapter position\n",
            encoding="utf-8")
    if validator_gd is not None:
        (addon / "validator.gd").write_text(validator_gd, encoding="utf-8")
    chosen = features if features is not None else {"demo": FEATURE}
    for stem, text in chosen.items():
        (addon / "features" / f"{stem}.feature").write_text(
            text, encoding="utf-8")
    if gut:
        (root / "addons" / "gut").mkdir(parents=True, exist_ok=True)
        (root / "addons" / "gut" / "gut_cmdln.gd").write_text(
            "# gut stub\n", encoding="utf-8")
    if gutconfig:
        (root / ".gutconfig.json").write_text(
            json.dumps({"dirs": ["tests"], "include_subdirs": True}),
            encoding="utf-8")
    return root


def ensure_addon_metadata(project: Path) -> None:
    """给已写入 features 的项目补 schema.json + harness.gd + validator.gd
    （schema 握手与 run 预检的最小合法元数据）。"""
    import json as _json
    addon = project / "addons" / "gqt_gherkin"
    addon.mkdir(parents=True, exist_ok=True)
    (addon / "features").mkdir(parents=True, exist_ok=True)
    s = addon / "schema.json"
    if not s.is_file():
        s.write_text(_json.dumps(VALID_SCHEMA, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    h = addon / "harness.gd"
    if not h.is_file():
        h.write_text("# harness stub — testing_api adapter position\n",
                     encoding="utf-8")
    v = addon / "validator.gd"
    if not v.is_file():
        v.write_text(VALIDATOR_OK_GD, encoding="utf-8")


def fake_validator_process(ok: bool = True,
                           supported_majors: list | None = None,
                           reason: str = ""):
    """argv 路由用共享 fake：validator 拉起回单行 verdict JSON。"""
    verdict = json.dumps({"ok": ok,
                          "supported_majors": (supported_majors
                                               if supported_majors is not None
                                               else [1]),
                          "reason": reason},
                         ensure_ascii=False)

    class _V:
        returncode = 0
        stdout = verdict + "\n"
        stderr = ""
    return _V()


def write_feature(project: Path, stem: str,
                  text: str = FEATURE) -> Path:
    """把 .feature 放进单根 features 目录（合法路径形态）。"""
    p = project / "addons" / "gqt_gherkin" / "features" / f"{stem}.feature"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def schema_of(project: Path) -> dict:
    return json.loads(
        (project / "addons" / "gqt_gherkin" / "schema.json").read_text(
            encoding="utf-8"))
