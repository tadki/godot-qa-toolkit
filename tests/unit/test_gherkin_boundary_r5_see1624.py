"""SEE-1624 C1-R5（Final Review §SPEC-019）— 六组边界缺口回归。

覆盖（每组先在 `305bcfa` 判红、修复后转绿）：
  R5-1 CRITICAL 所有权绑定：复制来的合法 marker 不得让调用方目录被删
  R5-2 HIGH addon schema/validator/harness 的 symlink 逃逸拒绝
  R5-3 HIGH 真实 Godot 启动一次性环境隔离（独立 HOME/XDG/TMPDIR，退出销毁）
  R5-4 HIGH 连字符 feature key 在 compile/group/reconcile 全链一致
  R5-5 MEDIUM worker 配置失败零残留
  R5-6 MEDIUM 退役 env 只要存在（含空值）即具名拒绝
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gherkin_fixtures import make_addon_project, write_feature


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = make_addon_project(tmp_path / "proj")
    write_feature(root, "demo")
    return root


# ============ R5-1 所有权绑定（CRITICAL） ============

def test_copied_owner_marker_never_deletes_caller_dir(
        project: Path, tmp_path: Path) -> None:
    """调用方备份目录内放入**合法 marker 副本** + 自己的文件 → 清理必须跳过，
    目录与文件均存活（所有权绑定：marker 内容须等于目录名且位于受管根）。"""
    from godot_qa_toolkit.gherkin.suite import _cleanup_suite_dir

    caller = project / "caller-backup"
    caller.mkdir()
    (caller / "notes.txt").write_text("caller data", encoding="utf-8")
    # 复制自一个真实私有套件的合法 marker（内容是**别的**目录名）
    (caller / ".gqt-owner").write_text(".gqt-run-someoneelse\n",
                                       encoding="utf-8")
    result = _cleanup_suite_dir(caller, project)
    assert result == "kept-caller-owned"
    assert caller.is_dir() and (caller / "notes.txt").is_file()


def test_marker_naming_own_dir_outside_managed_root_is_kept(
        project: Path, tmp_path: Path) -> None:
    """marker 内容与目录名一致但**不在受管暂存根**内 → 仍属调用方持有。"""
    from godot_qa_toolkit.gherkin.suite import _cleanup_suite_dir

    caller = project / ".gqt-run-abc123"
    caller.mkdir()
    (caller / ".gqt-owner").write_text(caller.name + "\n", encoding="utf-8")
    (caller / "keep.txt").write_text("x", encoding="utf-8")
    assert _cleanup_suite_dir(caller, project) == "kept-caller-owned"
    assert (caller / "keep.txt").is_file()


def test_tool_private_suite_still_cleaned(project: Path) -> None:
    """工具自建私有套件（受管形态 + 绑定 marker）成功路径仍零残留。"""
    from godot_qa_toolkit.gherkin.suite import _cleanup_suite_dir, create_suite_dir

    d = create_suite_dir(project)
    assert d.is_dir()
    assert _cleanup_suite_dir(d, project) == "removed"
    assert not d.exists()


# ============ R5-2 symlink 逃逸（HIGH） ============

def test_schema_symlink_escape_rejected(project: Path, tmp_path: Path) -> None:
    from godot_qa_toolkit.gherkin.addon import AddonError, compile_preflight

    schema = project / "addons" / "gqt_gherkin" / "schema.json"
    external = tmp_path / "external-schema.json"
    external.write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")
    schema.unlink()
    schema.symlink_to(external)
    with pytest.raises(AddonError, match="symlink"):
        compile_preflight(project)


def test_validator_symlink_escape_rejected(project: Path,
                                           tmp_path: Path) -> None:
    from godot_qa_toolkit.gherkin.addon import (
        AddonError, run_validator_preflight)

    validator = project / "addons" / "gqt_gherkin" / "validator.gd"
    external = tmp_path / "external-validator.gd"
    external.write_text("extends SceneTree\n", encoding="utf-8")
    validator.unlink()
    validator.symlink_to(external)
    with pytest.raises(AddonError, match="symlink"):
        run_validator_preflight(project)


def test_harness_symlink_escape_rejected(project: Path,
                                          tmp_path: Path) -> None:
    from godot_qa_toolkit.gherkin.compile import compile_feature

    harness = project / "addons" / "gqt_gherkin" / "harness.gd"
    external = tmp_path / "external-harness.gd"
    external.write_text("extends RefCounted\n", encoding="utf-8")
    harness.unlink()
    harness.symlink_to(external)
    suite, error = compile_feature(
        project / "addons" / "gqt_gherkin" / "features" / "demo.feature",
        project, write=False)
    assert suite is None and error is not None
    assert "symlink" in error["reason"]


# ============ R5-3 真实 Godot 启动环境隔离（HIGH） ============

def _capture_validator_env(project: Path, monkeypatch) -> dict:
    """截获 validator 启动时的 env（真实拉起参数走 fake）。"""
    import godot_qa_toolkit.gherkin.addon as addon_mod

    # 平台判定固定为 Linux 原生通道（宿主是 WSL，避免用例依赖宿主形态）
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)

    captured: dict = {}

    class _R:
        returncode = 0
        stdout = json.dumps({"ok": True, "supported_majors": [1],
                             "reason": ""}) + "\n"
        stderr = ""

    def fake_spawn(argv, **kwargs):
        captured.update(kwargs.get("env") or {})
        return _R()

    monkeypatch.setattr(addon_mod, "spawn_godot", fake_spawn)
    addon_mod.run_validator_preflight(project)
    return captured


def test_validator_launch_uses_isolated_env(project: Path,
                                           monkeypatch) -> None:
    env = _capture_validator_env(project, monkeypatch)
    assert env, "validator launch must carry an explicit env"
    real_home = os.path.expanduser("~")
    assert env.get("HOME") and env["HOME"] != real_home
    assert env.get("XDG_DATA_HOME") and env["XDG_DATA_HOME"] != os.environ.get(
        "XDG_DATA_HOME", "")
    assert env.get("TMPDIR") and env["TMPDIR"] != os.environ.get("TMPDIR", "")
    # Linux 原生通道（显式清除 WSL_DISTRO_NAME，本机宿主为 WSL）：
    # APPDATA/LOCALAPPDATA 属 win64 语义不设；WSLENV 原样继承。
    assert "APPDATA" not in env and "LOCALAPPDATA" not in env
    assert env.get("WSLENV") == os.environ.get("WSLENV")


def test_validator_isolated_env_destroyed_after(project: Path,
                                                monkeypatch) -> None:
    env = _capture_validator_env(project, monkeypatch)
    iso_home = Path(env["HOME"])
    assert not iso_home.exists(), "isolation dirs must be destroyed on exit"


def test_gut_run_uses_isolated_env(project: Path, monkeypatch) -> None:
    import godot_qa_toolkit.gherkin.addon as addon_mod
    import godot_qa_toolkit.gherkin.run as run_mod

    captured: dict = {}

    class _R:
        returncode = 0
        stdout = ("res://gherkin/generated/test_demo.gd\n"
                  "* test_x_000_00\n")
        stderr = ""

    def fake_spawn(argv, **kwargs):
        captured.update(kwargs.get("env") or {})
        return _R()

    class _Verdict:
        returncode = 0
        stdout = (json.dumps({"ok": True, "supported_majors": [1],
                              "reason": ""}) + "\n")
        stderr = ""

    # validator 阶段同样 stub（否则本用例依赖环境里的 Godot）
    monkeypatch.setattr(addon_mod, "spawn_godot",
                        lambda argv, **kwargs: _Verdict())
    monkeypatch.setattr(run_mod, "spawn_godot", fake_spawn)
    # 先 compile 产物
    from godot_qa_toolkit.gherkin.compile import compile_feature
    compile_feature(
        project / "addons" / "gqt_gherkin" / "features" / "demo.feature",
        project, write=True)
    run_mod.run_suite(project, "demo")
    assert captured, "GUT launch must carry an explicit env"
    assert captured.get("HOME") != os.path.expanduser("~")


# ============ R5-4 连字符 feature key 全链一致（HIGH） ============

def test_hyphenated_feature_key_chain_consistent(project: Path) -> None:
    """my-demo.feature → 产物键 my_demo；compile/group/reconcile 全链一致，
    不得按原 key 读 manifest（崩溃）。"""
    from godot_qa_toolkit.gherkin.compile import compile_feature
    from godot_qa_toolkit.gherkin.suite import compile_keys_to_suite, reconcile
    from godot_qa_toolkit.measurement.arms import _gherkin_groups

    write_feature(project, "my-demo")
    suite, error = compile_feature(
        project / "addons" / "gqt_gherkin" / "features" / "my-demo.feature",
        project, write=True)
    assert error is None
    assert suite.feature_key == "my_demo"
    assert (project / "gherkin" / "generated" /
            "my_demo.manifest.json").is_file()

    # 分组口径与编译口径一致
    groups = _gherkin_groups(["my-demo"], 1)
    assert groups == [["my_demo"]]

    # 私有套件编译 + 对账（按归一键）不得抛 FileNotFoundError
    from godot_qa_toolkit.gherkin.suite import create_suite_dir
    suite_dir = create_suite_dir(project)
    compile_keys_to_suite(project, ["my-demo"], suite_dir)
    manifest = json.loads(
        (suite_dir / "generated" / "my_demo.manifest.json").read_text(
            encoding="utf-8"))
    flow = [f"res://{suite_dir.name}/generated/test_my_demo.gd"]
    flow += [f"* {a['test_name']}" for a in manifest["attribution"]]
    record = reconcile(project, suite_dir, ["my-demo"], "\n".join(flow),
                       replay_ok=True)
    assert record["ok"] is True


# ============ R5-5 worker 配置失败零残留（MEDIUM） ============

def _iso_residue() -> set[str]:
    """隔离残留：产品现用 `gqt-godot-iso-*`（历史命名 gqt-gherkin-worker-*
    一并纳入，避免命名漂移再次造成假绿）。"""
    import glob
    import tempfile as _tf

    root = _tf.gettempdir()
    found: set[str] = set()
    for pattern in ("gqt-godot-iso-*", "gqt-gherkin-worker-*"):
        found |= set(glob.glob(os.path.join(root, pattern)))
    return found


def test_iso_residue_probe_has_discriminating_power(tmp_path: Path) -> None:
    """判别力自检：预置一个 `gqt-godot-iso-*` 目录必须被残留探测捕获——
    若 glob 仍查旧名 `gqt-gherkin-worker-*`，本用例判红。"""
    import tempfile as _tf

    planted = Path(_tf.gettempdir()) / f"gqt-godot-iso-probe-{os.getpid()}"
    planted.mkdir(exist_ok=True)
    try:
        assert str(planted) in _iso_residue()
    finally:
        planted.rmdir()


def test_worker_config_failure_leaves_no_residue(project: Path,
                                                monkeypatch,
                                                tmp_path: Path) -> None:
    """worker 前奏在二进制配置错误（BinaryConfigError，非 OSError）时也必须
    零残留：scratch / 隔离 HOME-XDG-TMPDIR 全部回收（§SPEC-019 MEDIUM-5）。"""
    import glob
    import tempfile as _tf

    import godot_qa_toolkit.gherkin.mutate_runner as mr

    # 先编译出 worker 产物（否则前奏在"产物缺失"处早退，触达不到配置错误面）
    from godot_qa_toolkit.gherkin.compile import compile_feature
    compile_feature(
        project / "addons" / "gqt_gherkin" / "features" / "demo.feature",
        project, write=True)
    monkeypatch.setenv("GQT_GODOT_BIN", "/nonexistent/godot-binary")
    before_iso = _iso_residue()
    before_scratch = set(glob.glob(os.path.join(str(project / ".qa-cache"),
                                              "gqt-gherkin-worker-*")))
    with pytest.raises(mr.GherkinRunError):
        mr._spawn_worker(project)
    after_iso = _iso_residue()
    after_scratch = set(glob.glob(os.path.join(str(project / ".qa-cache"),
                                             "gqt-gherkin-worker-*")))
    assert after_scratch == before_scratch, (
        f"worker scratch residue: {after_scratch - before_scratch}")
    assert after_iso == before_iso, (
        f"worker isolation residue: {after_iso - before_iso}")


# ============ R5-6 退役 env 存在即拒（MEDIUM） ============

@pytest.mark.parametrize("value", ["", " ", "0", "false"])
def test_retired_env_presence_fails_loud(monkeypatch, value: str) -> None:
    from godot_qa_toolkit.measurement import orchestrator as orch
    from godot_qa_toolkit.measurement.backup import MeasurementError

    monkeypatch.delenv("GQT_MEASUREMENT_GHERKIN_DISABLED", raising=False)
    monkeypatch.setenv("GQT_MEASUREMENT_ACCEPTANCE_DISABLED", value)
    with pytest.raises(MeasurementError, match="retired"):
        orch.gherkin_arm_disabled()


def test_new_env_controls_pause(monkeypatch) -> None:
    from godot_qa_toolkit.measurement import orchestrator as orch

    monkeypatch.delenv("GQT_MEASUREMENT_ACCEPTANCE_DISABLED", raising=False)
    monkeypatch.setenv("GQT_MEASUREMENT_GHERKIN_DISABLED", "0")
    assert orch.gherkin_arm_disabled() is False
    monkeypatch.setenv("GQT_MEASUREMENT_GHERKIN_DISABLED", "1")
    assert orch.gherkin_arm_disabled() is True


# ============ R5-7 addon 根/父路径 symlink 逃逸（HIGH-2 补漏） ============

def test_addon_root_symlink_escape_rejected(tmp_path: Path,
                                            project: Path) -> None:
    """整个 addons/gqt_gherkin 指向项目外 → 必须 fail-loud，不得经
    "已被同一逃逸解析后的允许根"相互比较而放行。"""
    from godot_qa_toolkit.gherkin.addon import AddonError, compile_preflight

    outside = tmp_path / "outside-addon"
    outside.mkdir()
    for name in ("schema.json", "validator.gd", "harness.gd"):
        (outside / name).write_text(
            (project / "addons" / "gqt_gherkin" / name).read_text(
                encoding="utf-8"), encoding="utf-8")
    (outside / "features").mkdir()
    addon = project / "addons" / "gqt_gherkin"
    import shutil
    shutil.rmtree(addon)
    addon.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AddonError, match="symlink|escapes"):
        compile_preflight(project)


def test_addons_parent_symlink_escape_rejected(tmp_path: Path,
                                               project: Path) -> None:
    """父目录 addons/ 指向项目外（addon 根自身不是 symlink）→ 同样拒绝。"""
    from godot_qa_toolkit.gherkin.addon import AddonError, compile_preflight

    outside = tmp_path / "outside-parent"
    outside.mkdir()
    import shutil
    shutil.move(str(project / "addons" / "gqt_gherkin"),
                str(outside / "gqt_gherkin"))
    shutil.rmtree(project / "addons")
    (project / "addons").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AddonError, match="symlink|escapes"):
        compile_preflight(project)


def test_normal_addon_root_still_accepted(project: Path) -> None:
    """正常项目内 addon 不受影响（回归护栏）。"""
    from godot_qa_toolkit.gherkin.addon import compile_preflight

    doc = compile_preflight(project)
    assert doc["schema"].startswith("gqt-gherkin-addon/")


# ============ R5-8 win64 通道 WSLENV 传播（HIGH-3 补漏） ============

def _iso_env_wsl(monkeypatch) -> dict:
    import godot_qa_toolkit.gherkin.isolation as iso_mod

    monkeypatch.setenv("WSL_DISTRO_NAME", "TestDistro")
    with iso_mod.isolated_godot_env() as env:
        return dict(env)


def test_wsl_isolation_propagates_wslenv(monkeypatch) -> None:
    """WSL：**仅 Windows 侧变量**经 WSLENV 放行（/w）且为 UNC 形态；
    Linux 侧变量保持绝对 Linux 路径、不进 WSLENV；不得透传宿主
    Windows 用户路径。"""
    monkeypatch.delenv("WSLENV", raising=False)
    monkeypatch.setenv("APPDATA", "C:\\Users\\real-user\\AppData\\Roaming")
    env = _iso_env_wsl(monkeypatch)
    wslenv = env.get("WSLENV", "")
    for name in ("APPDATA", "LOCALAPPDATA", "TMP", "TEMP"):
        assert f"{name}/w" in wslenv, f"{name} must be shared to Windows side"
        assert env[name].startswith("\\\\wsl.localhost\\TestDistro\\"), \
            f"{name} must be a Windows-side UNC path: {env[name]}"
        assert "real-user" not in env[name], "host Windows user path leaked"
    # Linux 侧变量：绝对 Linux 路径，且不出现在 WSLENV
    for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                 "XDG_CACHE_HOME", "TMPDIR"):
        assert env[name].startswith("/"), \
            f"{name} must stay an absolute Linux path in WSL: {env[name]}"
        assert f"{name}/w" not in wslenv, \
            f"Linux-side {name} must not be shared to Windows side"


def test_wsl_linux_side_xdg_isolated_native(monkeypatch) -> None:
    """WSL 内 Linux 原生 Godot：XDG_*/HOME/TMPDIR 指向一次性隔离目录
    （绝对 Linux 路径），不得回落到真实用户目录或项目目录。"""
    env = _iso_env_wsl(monkeypatch)
    real_home = os.path.expanduser("~")
    for name in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                 "XDG_CACHE_HOME", "TMPDIR"):
        value = env[name]
        assert value.startswith("/"), f"{name} not absolute: {value}"
        assert value != real_home, f"{name} must not be the real user dir"
        assert "\\" not in value, f"{name} must not carry Windows separators"
        assert value.startswith(env["HOME"].rsplit("/", 1)[0]) or \
            name == "HOME", f"{name} must live under the isolation root: {value}"


def test_wsl_wslenv_appends_existing(monkeypatch) -> None:
    """调用方已有 WSLENV 时追加不覆写（与 inject 同款 LOW-4 纪律）。"""
    monkeypatch.setenv("WSLENV", "FOO/p")
    env = _iso_env_wsl(monkeypatch)
    assert env["WSLENV"].startswith("FOO/p:")
    assert "APPDATA/w" in env["WSLENV"]


def test_linux_isolation_leaves_wslenv_untouched(monkeypatch) -> None:
    """Linux 原生通道不引入 WSLENV 副作用（不清空、不追加）。"""
    import godot_qa_toolkit.gherkin.isolation as iso_mod

    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setenv("WSLENV", "FOO/p")
    with iso_mod.isolated_godot_env() as env:
        assert env["WSLENV"] == "FOO/p"


# ============ R5-9 所有权绑定当前项目根（DEF-1） ============

def test_same_shape_backup_in_project_is_not_owned(project: Path) -> None:
    """项目内 `<project>/backup/gherkin/generated/.gqt-run-copy7`：目录名、
    marker 内容、父/祖父目录名全部形似受管暂存根，但不是**当前项目**的
    受管根 → 调用方持有，永不清除。"""
    from godot_qa_toolkit.gherkin.addon import is_owned_suite_dir
    from godot_qa_toolkit.gherkin.suite import _cleanup_suite_dir

    decoy = (project / "backup" / "gherkin" / "generated" / ".gqt-run-copy7")
    decoy.mkdir(parents=True)
    (decoy / ".gqt-owner").write_text(decoy.name + "\n", encoding="utf-8")
    (decoy / "notes.txt").write_text("backup data", encoding="utf-8")

    assert is_owned_suite_dir(decoy, project) is False
    assert _cleanup_suite_dir(decoy, project) == "kept-caller-owned"
    assert decoy.is_dir()
    assert (decoy / "notes.txt").read_text(encoding="utf-8") == "backup data"


def test_managed_root_of_other_project_is_not_owned(project: Path,
                                                    tmp_path: Path) -> None:
    """另一个项目的受管暂存根同样不算本项目的工具自有目录。"""
    from godot_qa_toolkit.gherkin.addon import is_owned_suite_dir
    from godot_qa_toolkit.gherkin.suite import create_suite_dir

    other = make_addon_project(tmp_path / "other-proj")
    d = create_suite_dir(other)
    assert is_owned_suite_dir(d, other) is True
    assert is_owned_suite_dir(d, project) is False


# ============ R5-10 项目外 suite-dir 入口具名失败（DEF-2） ============

def test_run_suite_dir_outside_project_fails_named(project: Path,
                                                   tmp_path: Path) -> None:
    """项目外 suite-dir：在 manifest 读取/replay/validator/Godot 之前以
    SuiteError 具名失败（不得裸 ValueError 逃逸）。"""
    from godot_qa_toolkit.gherkin.suite import SuiteError, run_suite_dir

    outside = (tmp_path / "outside-suite")
    (outside / "generated").mkdir(parents=True)
    (outside / "generated" / "demo.manifest.json").write_text(
        "{}", encoding="utf-8")
    (outside / "caller.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(SuiteError, match="outside the project root"):
        run_suite_dir(project, outside)
    assert (outside / "caller.txt").read_text(encoding="utf-8") == "keep"


def test_cli_outside_suite_dir_single_json(project: Path,
                                           tmp_path: Path) -> None:
    """公开面：项目外 --suite-dir → stdout 单 JSON（ok=false、suite_error），
    无 traceback，caller 文件不动。"""
    outside = (tmp_path / "outside-suite")
    (outside / "generated").mkdir(parents=True)
    (outside / "generated" / "demo.manifest.json").write_text(
        "{}", encoding="utf-8")
    (outside / "caller.txt").write_text("keep", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(Path("src").resolve()))
    r = subprocess.run(
        [sys.executable, "-m", "godot_qa_toolkit.cli", "gherkin", "run",
         "--project-root", str(project), "--suite-dir", str(outside)],
        capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode != 0
    payload = json.loads(r.stdout)  # 单 JSON、可解析
    assert payload["ok"] is False
    assert "outside the project root" in payload["summary"]["error"]
    assert "Traceback" not in r.stderr
    assert (outside / "caller.txt").read_text(encoding="utf-8") == "keep"


# ============ R5-11 diagnose 连字符 key 归一（MEDIUM） ============

def test_diagnose_accepts_hyphen_key(monkeypatch) -> None:
    """recipe 键为归一形态 my_demo 时，--features my-demo 必须被接受；
    未知键仍具名拒绝。"""
    import godot_qa_toolkit.measurement.diagnose as diag_mod
    from godot_qa_toolkit.measurement.backup import MeasurementError

    # 本用例只验证 key 归一；recipe 结构合同由既有 recipe 测试覆盖。
    monkeypatch.setattr(diag_mod, "verify_recipe", lambda r: (True, ""))
    recipe = {"gherkin_face": {"features": [
        {"key": "my_demo", "source": "res://addons/gqt_gherkin/features/"
                                      "my-demo.feature"}]}}
    feature_by_key, _sources = diag_mod._diagnose_prepare(
        recipe, ["my-demo"], [])
    assert "my_demo" in feature_by_key
    with pytest.raises(MeasurementError, match="unknown gherkin features"):
        diag_mod._diagnose_prepare(recipe, ["no-such"], [])


# ============ R5-12 symlink loop 具名域错误（MEDIUM） ============

def test_addon_symlink_loop_is_named(tmp_path: Path) -> None:
    from godot_qa_toolkit.gherkin.addon import AddonError, compile_preflight

    loop = tmp_path / "loop-project"
    loop.symlink_to(loop)
    with pytest.raises(AddonError, match="resolved|symlink loop"):
        compile_preflight(loop)


def test_suite_dir_symlink_loop_is_named(project: Path,
                                         tmp_path: Path) -> None:
    from godot_qa_toolkit.gherkin.suite import (
        SuiteError, ensure_suite_dir_in_project)

    loop = tmp_path / "loop-suite"
    loop.symlink_to(loop)
    with pytest.raises(SuiteError, match="resolved|symlink loop"):
        ensure_suite_dir_in_project(project, loop)


# ============ R5-13 非 UTF-8 ownership marker（MEDIUM） ============

def test_non_utf8_marker_treated_as_caller_owned(project: Path) -> None:
    """不可信 marker（非 UTF-8）→ 视为 caller-owned 并保留，清理不得破裂。"""
    from godot_qa_toolkit.gherkin.addon import is_owned_suite_dir
    from godot_qa_toolkit.gherkin.suite import _cleanup_suite_dir

    d = project / "gherkin" / "generated" / ".gqt-run-badmarker"
    d.mkdir(parents=True)
    (d / ".gqt-owner").write_bytes(b"\xff\xfe")
    (d / "notes.txt").write_text("keep", encoding="utf-8")
    assert is_owned_suite_dir(d, project) is False
    assert _cleanup_suite_dir(d, project) == "kept-caller-owned"
    assert (d / "notes.txt").read_text(encoding="utf-8") == "keep"
