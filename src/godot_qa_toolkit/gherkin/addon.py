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
from .isolation import isolated_godot_env
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

_SCHEMA_RE = re.compile(r"^gqt-gherkin-addon/(\d+)(?:\.(\d+))?$")
_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


SUITE_OWNER_MARKER = ".gqt-owner"
_PRIVATE_SUITE_RE = re.compile(r"^\.gqt-run-[A-Za-z0-9_.-]{1,64}$")


def is_owned_suite_dir(suite_dir: str | Path) -> bool:
    """私有套件目录的**工具所有权绑定**（§SPEC-019 CRITICAL-1）。

    三重条件全中才算工具自有：
    1. `.gqt-owner` 是普通文件（非 symlink），且**内容 == 本目录名**——
       复制来的合法 marker 内容指向原目录，与调用方目录名不符 → 永不删；
    2. 目录名形如 `.gqt-run-<token>`；
    3. 位置在受管暂存根内：`<project>/gherkin/generated/`。

    任一不中即视为调用方持有（`--suite-dir`/`--out-dir`/备份目录），
    清理一律跳过——调用方文件与目录永不被递归删除。"""
    sd = Path(suite_dir)
    marker = sd / SUITE_OWNER_MARKER
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        token = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if token != sd.name:
        return False
    if not _PRIVATE_SUITE_RE.match(sd.name):
        return False
    if sd.parent.name != "generated" or sd.parent.parent.name != "gherkin":
        return False
    return True


def _addon_physical_root(project_root: str | Path) -> Path:
    """addon 的**物理根**：addon 根自身及父路径不得逃出项目根。

    §SPEC-019 HIGH-2 补漏（R5）：不能以 `(root / ADDON_DIR_RELPATH).resolve()`
    作为允许根——addon 根本身是 symlink 时该值随之逃到项目外，两个"已被
    同一逃逸解析后的路径"相互比较恒真。此处以**项目物理根**为基准独立
    判定：addon 根不得是 symlink，其父路径（如 `addons/`）亦不得经 symlink
    把物理位置带出项目根。"""
    root = Path(project_root).resolve()
    addon_root = root / ADDON_DIR_RELPATH
    if addon_root.is_symlink():
        raise AddonError(
            f"addon root must not be a symlink: {addon_root} — the strict "
            f"single root requires a physical {ADDON_DIR_RELPATH}/ inside the "
            f"project")
    resolved = addon_root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise AddonError(
            f"addon root escapes the project root: {resolved} — parent path "
            f"symlinks may not move {ADDON_DIR_RELPATH}/ outside the project")
    return resolved


def addon_file(project_root: str | Path, relpath: str, what: str) -> Path:
    """addon 内文件的物理位置校验：不得是 symlink，且解析后仍在 addon 根内。

    §SPEC-019 HIGH-2：schema / validator / harness 经 symlink 逃逸严格单根
    一律 fail-loud（标准 addon 内的正常文件不受影响）；整个 addon 根或其
    父路径逃逸同样拒绝（R5 补漏，独立物理根判定）。"""
    root = Path(project_root).resolve()
    addon_root = _addon_physical_root(root)
    path = root / relpath
    if path.is_symlink():
        raise AddonError(
            f"{what} must not be a symlink: {path} — the strict single root "
            f"rejects files that escape {ADDON_DIR_RELPATH}/")
    resolved = path.resolve()
    try:
        resolved.relative_to(addon_root)
    except ValueError:
        raise AddonError(
            f"{what} escapes the addon root: {path} — the strict single root "
            f"rejects files that resolve outside {ADDON_DIR_RELPATH}/") from None
    return path


def normalize_feature_key(stem: str) -> str:
    """feature key 全链唯一规范化：非 [A-Za-z0-9_] → `_`。

    编译文件名（test_<key>.gd）、manifest.feature_key、suite/replay 的
    key 解析一律经此（§SPEC-005 修复：compile 归一而 suite 按原 key 查
    manifest 会使带连字符 feature 的 key-mode 运行必失败）。"""
    return _KEY_UNSAFE.sub("_", str(stem))


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
    target = normalize_feature_key(key)
    matches = [p for p in hits if normalize_feature_key(p.stem) == target]
    if len(matches) > 1:
        raise AddonError(
            f"duplicate feature key after normalization: {key!r} matches "
            f"{[m.name for m in matches]} — keys must be 1:1 with sources")
    if matches:
        return matches[0]
    raise AddonError(
        f"unknown key: {key} — no {FEATURES_DIR_RELPATH} entry with that "
        f"stem")


def read_schema(project_root: str | Path) -> dict:
    """读 addon schema.json：缺声明/损坏/非对象具名拒绝（纯解析，零 Godot）。"""
    path = addon_file(project_root, SCHEMA_RELPATH, "addon schema.json")
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
    # R2 批准字段（§SPEC-019 终稿）：`schema`（`gqt-gherkin-addon/<major>
    # [.<minor>]`：major=破坏性、minor=加法）、`requires_gqt`、`testing_api`；
    # 未知键忽略（加性演进）。
    doc = read_schema(project_root)
    version = doc.get("schema")
    if not isinstance(version, str):
        raise AddonError(
            f"addon schema missing 'schema' declaration: {doc!r} "
            f"(expected {SCHEMA_VERSION})")
    m = _SCHEMA_RE.match(version)
    if m is None or int(m.group(1)) not in SUPPORTED_ADDON_MAJORS:
        raise AddonError(
            f"unsupported addon schema major: {version!r} — this GQT "
            f"supports exactly {list(SUPPORTED_ADDON_MAJORS)} "
            f"({SCHEMA_VERSION}; minor additive versions are accepted)")
    req_gqt = doc.get("requires_gqt")
    if not isinstance(req_gqt, str):
        raise AddonError(
            f"addon schema requires_gqt declaration missing/invalid: "
            f"got {req_gqt!r} — must declare 'requires_gqt'")
    if _version_tuple(req_gqt) > _version_tuple(GQT_VERSION):
        raise AddonError(
            f"addon requires_gqt {req_gqt!r} is higher than this GQT "
            f"({GQT_VERSION}) — upgrade the toolkit or lower the addon "
            f"requirement")
    req_token = doc.get("testing_api")
    if not isinstance(req_token, str) or not req_token:
        raise AddonError(
            f"addon schema testing_api declaration missing/invalid: "
            f"got {req_token!r} — must declare 'testing_api'")
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
    validator_path = addon_file(project_root, VALIDATOR_RELPATH,
                                "addon validator.gd")
    if not validator_path.is_file():
        raise AddonError(
            f"addon validator missing: {validator_path} — run/mutate "
            f"must execute the addon's own validator.gd before any "
            f"business Godot process")
    try:
        # 一次性隔离环境（§SPEC-019 HIGH-3）：validator 不得触碰真实用户
        # 目录——隔离由产品构造保证，不依赖调用方外部设置 HOME。
        with isolated_godot_env() as iso_env:
            with GlobalGodotGate(str(project_root)):
                r = spawn_godot(
                    [godot_command(), "--headless", "--path",
                     str(project_root), "-s", VALIDATOR_RES],
                    capture_output=True, text=True, timeout=timeout_s,
                    cwd=str(project_root), env=iso_env)
    except Exception as e:  # noqa: BLE001 — 拉起失败统一具名（含超时/BinaryConfigError）
        raise AddonError(
            f"addon validator launch failed ({type(e).__name__}): {e}") from e
    output = (r.stdout or "") + "\n" + (r.stderr or "")
    if r.returncode != 0:
        raise AddonError(
            f"addon validator process failed (exit={r.returncode}) — a "
            f"non-zero validator exit is rejected regardless of printed "
            f"verdicts (tail: {' / '.join(output.splitlines()[-3:])})")
    # 协议身份 + 最后一行为准：正向扫描持续覆盖，最终只认**最后一个**
    # 携带 supported_majors 的合法 verdict——早期噪声/日志绿 verdict 不得
    # 覆盖其后真正的红判定（反之亦然）。
    verdict: dict | None = None
    for line in output.splitlines():
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
