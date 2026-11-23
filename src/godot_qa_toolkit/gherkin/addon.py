"""`addons/gqt_gherkin/` 严格单根契约与 schema/validator 握手（SEE-1624）。

唯一解析点（R2 §3）：features 发现、harness 加载位、validator 位置与
schema 握手全部由本模块承载——suite 的 feature 解析、recipe 扫描、
生成模板与 arms 共用，禁止多处漂移。旧布局（旧 addon 根与项目侧旧
acceptance 目录）不再是合法输入：缺根/空集/缺声明 fail-loud，无双根、
无回退、无自动复制（Owner 2026-11-23 单根裁决）。

两级预检（§SPEC-003，破坏性、无旁路）：
- compile_preflight：纯解析握手——schema.json 存在、`gqt-gherkin-addon/1`
  major 受支持、requires.gqt 不高于本工具、requires.testing_api 与本
  工具 testing-api token 一致；零 Godot 进程。
- run_validator_preflight：拉起 addon 自带 `validator.gd` 一次，断言
  其 `supported_majors` 与本工具支持集完全相等（漂移即拒）。
调用方按「纯解析握手 → 纯域校验（replay）→ validator 阶段 → 业务
执行」装配——域错误不被缺二进制的环境错误抢占（§SPEC-016）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..mutation.godot_gate import GlobalGodotGate
from ..mutation.inject import godot_command, spawn_godot

SCHEMA_VERSION = "gqt-gherkin-addon/1"
# 本 GQT 理解的 addon schema major 集：漂移（多/少/不同）一律拒绝——
# 版本协商取交集语义会让"半兼容"静默通过，违背 fail-loud 纪律。
SUPPORTED_ADDON_MAJORS = (1,)
# 本工具自身版本（与 pyproject / 发布 tag 同步）：schema requires.gqt 的
# 比较基准。发布 v0.4.0 起由本常量承载，不读元数据（可测试、零依赖）。
GQT_VERSION = "v0.4.0"

ADDON_DIR_RELPATH = "addons/gqt_gherkin"
FEATURES_DIR_RELPATH = f"{ADDON_DIR_RELPATH}/features"
HARNESS_RES = "res://addons/gqt_gherkin/harness.gd"
VALIDATOR_RES = "res://addons/gqt_gherkin/validator.gd"
VALIDATOR_RELPATH = f"{ADDON_DIR_RELPATH}/validator.gd"
SCHEMA_RELPATH = f"{ADDON_DIR_RELPATH}/schema.json"
SCHEMA_FILENAME = "schema.json"

_SCHEMA_MAJOR_RE = re.compile(r"^gqt-gherkin-addon/(\d+)$")


class AddonError(Exception):
    """单根布局或 schema/validator 握手违例——具名 fail-loud，无静默回退。"""


def _testing_api_token() -> str:
    # 延迟导入避免环：compile.py 依赖本模块预检，而 token 归属包命名空间。
    from . import OPS_VERSION
    return OPS_VERSION


def features_root(project_root: str | Path) -> Path:
    return Path(project_root) / ADDON_DIR_RELPATH / "features"


def resolve_feature(project_root: str | Path, key: str) -> Path:
    """单根 features 发现：缺根/空集 fail-loud（R2 §3 错误串直指单根），
    否则按 stem 返回唯一 .feature；未知 key 具名拒绝。"""
    feats = features_root(project_root)
    if not feats.is_dir():
        raise AddonError(
            f"no .feature discovered at {FEATURES_DIR_RELPATH} — gqt only "
            f"loads the strict single root `{ADDON_DIR_RELPATH}/` "
            f"(missing features root: {feats})")
    hits = sorted(feats.glob("*.feature"))
    if not hits:
        raise AddonError(
            f"no .feature discovered at {FEATURES_DIR_RELPATH} — empty "
            f"feature set is not a valid addon layout")
    for p in hits:
        if p.stem == key:
            return p
    raise AddonError(
        f"unknown key: {key} — no {FEATURES_DIR_RELPATH} entry with that "
        f"stem")


def read_schema(project_root: str | Path) -> dict:
    """读 addon schema.json：缺声明/损坏/非对象具名拒绝（纯解析，零 Godot）。"""
    path = Path(project_root) / SCHEMA_RELPATH
    if not path.is_file():
        raise AddonError(
            f"addon schema declaration missing: {path} — every "
            f"{ADDON_DIR_RELPATH}/ addon must declare {SCHEMA_FILENAME} "
            f"({SCHEMA_VERSION})")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise AddonError(
            f"addon schema unparseable: {path} "
            f"({type(e).__name__}: {e})") from e
    if not isinstance(doc, dict):
        raise AddonError(
            f"addon schema must be a JSON object, got "
            f"{type(doc).__name__}: {path}")
    return doc


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", v)
    if not parts:
        raise AddonError(f"unparseable version literal: {v!r}")
    return tuple(int(x) for x in parts)


def compile_preflight(project_root: str | Path) -> dict:
    """compile 侧纯解析握手（§SPEC-003）：schema 缺声明、major 不支持、
    requires 过高、testing-api token 漂移具名拒绝；零 Godot 进程。
    返回通过核验的 schema dict。"""
    doc = read_schema(project_root)
    version = doc.get("schema_version")
    if not isinstance(version, str):
        raise AddonError(
            f"addon schema missing schema_version: {doc!r} "
            f"(expected {SCHEMA_VERSION})")
    m = _SCHEMA_MAJOR_RE.match(version)
    if m is None or int(m.group(1)) not in SUPPORTED_ADDON_MAJORS:
        raise AddonError(
            f"unsupported addon schema major: {version!r} — this GQT "
            f"supports exactly {list(SUPPORTED_ADDON_MAJORS)} "
            f"({SCHEMA_VERSION})")
    requires = doc.get("requires")
    if not isinstance(requires, dict) or "gqt" not in requires \
            or "testing_api" not in requires:
        raise AddonError(
            f"addon schema requires declaration missing/incomplete: "
            f"got {requires!r} — must declare 'gqt' and 'testing_api'")
    req_gqt = requires["gqt"]
    if not isinstance(req_gqt, str):
        raise AddonError(
            f"addon schema requires.gqt must be a string, got "
            f"{req_gqt!r}")
    if _version_tuple(req_gqt) > _version_tuple(GQT_VERSION):
        raise AddonError(
            f"addon requires.gqt {req_gqt!r} is higher than this GQT "
            f"({GQT_VERSION}) — upgrade the toolkit or lower the addon "
            f"requirement")
    req_token = requires["testing_api"]
    expected = _testing_api_token()
    if req_token != expected:
        raise AddonError(
            f"addon schema testing_api token {req_token!r} does not match "
            f"this GQT's testing-api contract {expected!r} — supported-set "
            f"drift is rejected (recompile/re-pin the addon)")
    return doc


def run_validator_preflight(project_root: str | Path,
                            timeout_s: int = 60) -> dict:
    """validator 拉起阶段（§SPEC-003）：业务执行前拉起 addon 内同一
    `validator.gd` 一次；verdict `ok=false`（V/P 检查红）、
    `supported_majors` 与本工具支持集不完全相等（漂移）、verdict 缺失
    或不可解析均具名拒绝。返回 validator verdict dict。

    与 `compile_preflight`（纯解析）分离：调用方按 纯解析握手 → 纯域
    校验（replay）→ 本阶段 → 业务执行 的次序装配——replay 等不需要
    Godot 的领域检查不得被缺少二进制的环境错误抢占（§SPEC-016）。"""
    validator_path = Path(project_root) / VALIDATOR_RELPATH
    if not validator_path.is_file():
        raise AddonError(
            f"addon validator missing: {validator_path} — run/mutate "
            f"must execute the addon's own validator.gd before any "
            f"business Godot process")
    try:
        with GlobalGodotGate(str(project_root)):
            r = spawn_godot(
                [godot_command(), "--headless", "--path",
                 str(project_root), "-s", VALIDATOR_RES],
                capture_output=True, text=True, timeout=timeout_s,
                cwd=str(project_root))
    except Exception as e:  # noqa: BLE001 — 拉起失败统一具名（含超时/BinaryConfigError）
        raise AddonError(
            f"addon validator launch failed ({type(e).__name__}): {e}") from e
    output = (r.stdout or "") + "\n" + (r.stderr or "")
    verdict: dict | None = None
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "supported_majors" in parsed:
            verdict = parsed
    if verdict is None:
        raise AddonError(
            f"addon validator produced no parsable verdict JSON with "
            f"'supported_majors' (exit={r.returncode}; tail: "
            f"{' / '.join(output.splitlines()[-3:])})")
    if verdict.get("ok") is not True:
        raise AddonError(
            f"addon validator reported red checks: "
            f"{verdict.get('reason') or verdict.get('ok')!r}")
    reported = verdict.get("supported_majors")
    expected = list(SUPPORTED_ADDON_MAJORS)
    if reported != expected:
        raise AddonError(
            f"addon validator supported_majors {reported!r} does not equal "
            f"this GQT's supported set {expected!r} — supported-set drift "
            f"is rejected")
    return verdict
