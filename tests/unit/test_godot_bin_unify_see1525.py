"""SEE-1525 §SPEC-001/002/006：Godot 二进制定位单一解析源 + per-component
--godot-bin 覆盖 + 未设变量行为冻结（opt-in 红线）。

- §SPEC-001：coverage 启动点（coverage/runner.py 旧 :494 硬编码 "godot"）
  接入 godot_command() 同源解析——env 设定时不被 PATH 首匹配劫持。
- §SPEC-002：--godot-bin > GQT_GODOT_BIN > PATH 三级优先，mutation /
  coverage / accept run|mutate 同语义；--path 形态随 detect_binary_kind()
  同源（win64 /mnt/→wslpath 转换保留，linux 原样不转换）；executor 预热
  同源。
- §SPEC-006 冻结红线：未设 env 且未传 flag 时，解析结果与 af728f5 同款
  （PATH 首匹配 / 字面量 "godot" 兜底），coverage 子进程不新增注入键。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess as stdlib_subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from godot_qa_toolkit.coverage import runner as cov_runner
from godot_qa_toolkit.mutation import executor as mexec
from godot_qa_toolkit.mutation import inject
from godot_qa_toolkit.mutation import prewarm_proof as mproof
from godot_qa_toolkit.mutation import runner as mrunner

ELF_MAGIC = b"\x7fELF" + b"\x00" * 8


def _mk_binary(tmp_path: Path, name: str, content: bytes = ELF_MAGIC) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    p.chmod(0o755)
    return p


@pytest.fixture(autouse=True)
def _fresh_kind_cache():
    inject.reset_binary_kind_cache()
    yield
    inject.reset_binary_kind_cache()


@pytest.fixture(autouse=True)
def _env_guard(monkeypatch):
    # apply_godot_bin_override 直接写 os.environ——经 monkeypatch 登记恢复
    # 点，防真实全局 GQT_GODOT_BIN（~/.bashrc 导出）跨用例泄漏。
    monkeypatch.setenv("GQT_GODOT_BIN", os.environ.get("GQT_GODOT_BIN", ""))
    yield


def _mk_gut_project(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "addons" / "gut").mkdir(parents=True)
    (tmp_path / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    target = tmp_path / "calc.gd"
    target.write_text("func add(a, b):\n\treturn a + b\n", encoding="utf-8")
    return tmp_path, target


def _fake_gut_run(seen: dict):
    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="* test_ok", stderr="")
    return fake_run


class TestApplyGodotBinOverride:
    def test_flag_overrides_env(self, tmp_path, monkeypatch):
        elf_a = _mk_binary(tmp_path, "godot_a")
        elf_b = _mk_binary(tmp_path, "godot_b")
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf_a))
        inject.apply_godot_bin_override(str(elf_b))
        assert inject.godot_command() == str(elf_b)
        assert inject.detect_binary_kind() == "linux"

    def test_env_used_when_flag_absent(self, tmp_path, monkeypatch):
        elf_a = _mk_binary(tmp_path, "godot_a")
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf_a))
        inject.apply_godot_bin_override(None)
        assert inject.godot_command() == str(elf_a)

    def test_blank_explicit_raises(self):
        with pytest.raises(inject.BinaryConfigError, match="--godot-bin"):
            inject.apply_godot_bin_override("   ")

    def test_missing_path_raises_with_flag_label(self, tmp_path):
        with pytest.raises(inject.BinaryConfigError,
                           match=r"--godot-bin path does not exist"):
            inject.apply_godot_bin_override(str(tmp_path / "no_such"))

    def test_not_executable_raises(self, tmp_path):
        p = tmp_path / "noexec"
        p.write_bytes(ELF_MAGIC)
        with pytest.raises(inject.BinaryConfigError,
                           match=r"--godot-bin path is not executable"):
            inject.apply_godot_bin_override(str(p))


class TestPathFormSameSource:
    """--path 形态随 detect_binary_kind() 同源（HIGH 风险面）：linux 二进制
    不得收到 Windows 形式 --path，win64 通道转换行为逐字节保留。"""

    def test_win64_converts_mnt(self, monkeypatch):
        monkeypatch.setattr(inject, "wslpath_win", lambda p: "D:\\proj")
        assert inject.godot_project_path("/mnt/d/proj", kind="win64") == "D:\\proj"

    def test_linux_keeps_mnt_raw(self):
        assert inject.godot_project_path("/mnt/d/proj", kind="linux") == "/mnt/d/proj"

    def test_kind_none_resolves_via_detect(self, tmp_path, monkeypatch):
        elf = _mk_binary(tmp_path, "godot_lin")
        exe = _mk_binary(tmp_path, "Godot_win64.exe", b"MZ\x90\x00")
        monkeypatch.setattr(inject, "wslpath_win", lambda p: "D:\\proj")
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        assert inject.godot_project_path("/mnt/d/proj") == "/mnt/d/proj"
        monkeypatch.setenv("GQT_GODOT_BIN", str(exe))
        inject.reset_binary_kind_cache()
        assert inject.godot_project_path("/mnt/d/proj") == "D:\\proj"

    def test_relative_and_abs_non_mnt_untouched(self):
        assert inject.godot_project_path("rel/proj", kind="win64") == "rel/proj"
        assert inject.godot_project_path("/home/x/proj", kind="win64") == "/home/x/proj"
        assert inject.godot_project_path("rel/proj", kind="linux") == "rel/proj"

    def test_mutation_runner_delegates(self, tmp_path, monkeypatch):
        exe = _mk_binary(tmp_path, "Godot_win64.exe", b"MZ\x90\x00")
        elf = _mk_binary(tmp_path, "godot_lin")
        monkeypatch.setattr(inject, "wslpath_win", lambda p: "D:\\proj")
        monkeypatch.setenv("GQT_GODOT_BIN", str(exe))
        assert mrunner._godot_project_path("/mnt/d/proj") == "D:\\proj"
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        inject.reset_binary_kind_cache()
        assert mrunner._godot_project_path("/mnt/d/proj") == "/mnt/d/proj"

    def test_coverage_runner_delegates(self, tmp_path, monkeypatch):
        elf = _mk_binary(tmp_path, "godot_lin")
        exe = _mk_binary(tmp_path, "Godot_win64.exe", b"MZ\x90\x00")
        monkeypatch.setattr(inject, "wslpath_win", lambda p: "D:\\proj")
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        assert cov_runner._godot_project_path("/mnt/d/proj") == "/mnt/d/proj"
        monkeypatch.setenv("GQT_GODOT_BIN", str(exe))
        inject.reset_binary_kind_cache()
        assert cov_runner._godot_project_path("/mnt/d/proj") == "D:\\proj"


class TestCoverageWiring:
    """§SPEC-001：coverage 启动 argv 首元走 godot_command() 同源；§SPEC-006：
    未设变量时兜底形态与 af728f5 一致，子进程 env 注入键零新增。"""

    def _launch(self, tmp_path, monkeypatch) -> dict:
        root, target = _mk_gut_project(tmp_path / "proj")
        monkeypatch.setattr("godot_qa_toolkit.mutation.godot_gate.GlobalGodotGate",
                            contextlib.nullcontext)
        seen: dict = {}
        monkeypatch.setattr(cov_runner.subprocess, "run", _fake_gut_run(seen))
        outcome = cov_runner._run_gut_for_coverage(
            target, str(root), "original", "instrumented", 3, 80.0, 60)
        assert outcome is None
        return seen

    def test_env_set_uses_resolved_binary(self, tmp_path, monkeypatch):
        elf = _mk_binary(tmp_path / "bin", "godot_lin")
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        seen = self._launch(tmp_path, monkeypatch)
        assert seen["argv"][0] == str(elf)
        assert seen["argv"][0] != "godot"

    def test_unset_uses_path_first_match(self, tmp_path, monkeypatch):
        wrapper = tmp_path / "bin" / "godot"
        wrapper.parent.mkdir()
        wrapper.write_text("#!/bin/bash\nexec /mnt/d/G_w64.exe \"$@\"\n")
        wrapper.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))
        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        seen = self._launch(tmp_path, monkeypatch)
        assert seen["argv"][0] == str(wrapper)

    def test_unset_empty_path_falls_back_literal_af728f5(self, tmp_path,
                                                         monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        seen = self._launch(tmp_path, monkeypatch)
        assert seen["argv"][0] == "godot"

    def test_subprocess_env_not_injected_on_any_channel(self, tmp_path,
                                                        monkeypatch):
        elf = _mk_binary(tmp_path / "bin", "godot_lin")
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        seen = self._launch(tmp_path, monkeypatch)
        # coverage 通道既有语义 = 继承父 env（无 env kwarg）——任何通道下
        # 都不得新增注入键（§SPEC-1525-006 新增键冻结）。
        assert "env" not in seen["kwargs"]

    def test_path_form_linux_raw_under_env(self, tmp_path, monkeypatch):
        elf = _mk_binary(tmp_path / "bin", "godot_lin")
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        root, target = _mk_gut_project(tmp_path / "proj")
        monkeypatch.setattr("godot_qa_toolkit.mutation.godot_gate.GlobalGodotGate",
                            contextlib.nullcontext)
        seen: dict = {}
        monkeypatch.setattr(cov_runner.subprocess, "run", _fake_gut_run(seen))
        cov_runner._run_gut_for_coverage(
            target, str(root), "original", "instrumented", 3, 80.0, 60)
        # /tmp 项目根非 /mnt/ 前缀——两种通道都不转换（转换面只作用于
        # /mnt/ 绝对路径），此处钉 argv 内 --path 实参原样。
        assert seen["argv"][seen["argv"].index("--path") + 1] == str(root)


class TestPrewarmWiring:
    def _prewarm(self, tmp_path, monkeypatch) -> dict:
        root = tmp_path / "proj"
        root.mkdir()
        (root / "project.godot").write_text("config/name=\"t\"\n")
        seen: dict = {}
        # SEE-1571 A5 §SPEC-506 seam 重锚：patch 面落在 prewarm_proof 自身
        # 命名空间。A2 后预热已委托 prewarm_proof，旧 seam（mexec.subprocess）
        # 只是「executor 恰好还 import 着同一 stdlib 模块对象」的巧合依赖；
        # 本钉子不得因 executor 侧该 import 的去留而失效。
        monkeypatch.setattr(mproof, "subprocess", SimpleNamespace(
            run=_fake_gut_run(seen),
            TimeoutExpired=stdlib_subprocess.TimeoutExpired))
        mexec.prewarm_import(str(root))
        return seen

    def test_seam_does_not_depend_on_executor_subprocess_import(
            self, tmp_path, monkeypatch):
        """重锚实证 + LOW-1 收口（Final Review）：假替身只存在于
        prewarm_proof 命名空间，executor 不再携带 subprocess 导入。"""
        seen = self._prewarm(tmp_path, monkeypatch)
        assert seen["argv"][1:] == ["--headless", "--import", "--path",
                                    str(tmp_path / "proj")]
        assert mproof.subprocess.run.__name__ == "fake_run"
        assert not hasattr(mexec, "subprocess"), (
            "executor 不得再携带 subprocess 导入——LOW-1 死 seam 已删除，"
            "测试 patch 面唯一落在 prewarm_proof 命名空间")

    def test_env_set_uses_resolved_binary(self, tmp_path, monkeypatch):
        elf = _mk_binary(tmp_path / "bin", "godot_lin")
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        seen = self._prewarm(tmp_path, monkeypatch)
        assert seen["argv"][0] == str(elf)

    def test_unset_empty_path_falls_back_literal_af728f5(self, tmp_path,
                                                         monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        seen = self._prewarm(tmp_path, monkeypatch)
        assert seen["argv"][0] == "godot"

    def test_argv_tail_unchanged(self, tmp_path, monkeypatch):
        elf = _mk_binary(tmp_path / "bin", "godot_lin")
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        seen = self._prewarm(tmp_path, monkeypatch)
        root = tmp_path / "proj"
        assert seen["argv"][1:] == ["--headless", "--import", "--path",
                                    str(root)]


class TestCliGodotBin:
    def test_flag_registered_on_all_godot_launching_subcommands(self):
        from godot_qa_toolkit.cli import build_parser
        for argv in (
            ["mutation", "f.gd", "--project-root", "r", "--godot-bin", "/x"],
            ["coverage", "f.gd", "--project-root", "r", "--godot-bin", "/x"],
            ["accept", "run", "--project-root", "r", "--key", "k",
             "--godot-bin", "/x"],
            ["accept", "mutate", "--project-root", "r", "--key", "k",
             "--godot-bin", "/x"],
        ):
            args = build_parser().parse_args(argv)
            assert args.godot_bin == "/x"

    def test_compile_has_no_godot_bin(self):
        from godot_qa_toolkit.cli import build_parser
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["accept", "compile", "f.feature", "--project-root", "r",
                 "--godot-bin", "/x"])

    def _run_main(self, argv, capsys):
        from godot_qa_toolkit.cli import main
        rc = main(argv)
        out = capsys.readouterr().out
        return rc, json.loads(out)

    def test_mutation_invalid_override_contract(self, capsys):
        rc, payload = self._run_main(
            ["mutation", "f.gd", "--project-root", "r",
             "--godot-bin", "/no/such"], capsys)
        assert rc == 1
        assert payload["tool"] == "mutation"
        assert payload["ok"] is False
        assert "--godot-bin" in payload["summary"]["error"]

    def test_coverage_invalid_override_contract(self, capsys):
        rc, payload = self._run_main(
            ["coverage", "f.gd", "--project-root", "r",
             "--godot-bin", "/no/such"], capsys)
        assert rc == 1
        assert payload["tool"] == "coverage"
        assert payload["ok"] is False
        assert "--godot-bin" in payload["summary"]["error"]

    def test_accept_run_invalid_override_contract(self, capsys):
        rc, payload = self._run_main(
            ["accept", "run", "--project-root", "r", "--key", "k",
             "--godot-bin", "/no/such"], capsys)
        assert rc == 1
        assert payload["tool"] == "accept"
        assert payload["ok"] is False
        assert "--godot-bin" in payload["summary"]["error"]

    def test_accept_mutate_invalid_override_contract(self, capsys):
        rc, payload = self._run_main(
            ["accept", "mutate", "--project-root", "r", "--key", "k",
             "--godot-bin", "/no/such"], capsys)
        assert rc == 1
        assert payload["tool"] == "accept"
        assert payload["ok"] is False
        assert "--godot-bin" in payload["summary"]["error"]

    def test_coverage_flag_visible_to_runner(self, tmp_path, capsys,
                                             monkeypatch):
        elf = _mk_binary(tmp_path / "bin", "godot_lin")
        target = tmp_path / "f.gd"
        target.write_text("func a():\n\tpass\n")
        captured: dict = {}

        def fake_run_coverage(**kwargs):
            captured["env_bin"] = os.environ.get("GQT_GODOT_BIN")
            return {"tool": "coverage", "ok": True, "summary": {},
                    "failures": []}

        monkeypatch.setattr(cov_runner, "run_coverage", fake_run_coverage)
        rc, payload = self._run_main(
            ["coverage", str(target), "--project-root", str(tmp_path),
             "--godot-bin", str(elf)], capsys)
        assert rc == 0
        assert payload["ok"] is True
        assert captured["env_bin"] == str(elf)

    def test_accept_run_flag_visible_to_runner(self, tmp_path, capsys,
                                               monkeypatch):
        from godot_qa_toolkit.accept import run as accept_run
        elf = _mk_binary(tmp_path / "bin", "godot_lin")
        captured: dict = {}

        def fake_run_suite(root, feature_key, timeout_s=120):
            captured["env_bin"] = os.environ.get("GQT_GODOT_BIN")
            return {"tool": "accept", "ok": True, "summary": {},
                    "failures": []}

        monkeypatch.setattr(accept_run, "run_suite", fake_run_suite)
        rc, payload = self._run_main(
            ["accept", "run", "--project-root", str(tmp_path), "--key", "k",
             "--godot-bin", str(elf)], capsys)
        assert rc == 0
        assert captured["env_bin"] == str(elf)


class TestFreezeUnset:
    """§SPEC-006：未设变量路径冻结——win64 env 通道键集与 af728f5 一致，
    未传 flag 时 CLI 不触碰 GQT_GODOT_BIN。"""

    def test_mutant_env_win64_keyset_frozen(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setattr(inject, "wslpath_win", lambda p: "D:\\" + p)
        env_extra, cfg = inject.mutant_env(str(tmp_path / "cfg.json"),
                                           str(tmp_path / "user"))
        assert set(env_extra) == {"XDG_DATA_HOME", "APPDATA",
                                  "GQT_MUTATION_CFG", "WSLENV"}

    def test_unset_cli_leaves_env_untouched(self, tmp_path, capsys,
                                            monkeypatch):
        from godot_qa_toolkit.cli import main
        target = tmp_path / "f.gd"
        target.write_text("func a():\n\tpass\n")
        captured: dict = {}

        def fake_run_coverage(**kwargs):
            captured["had_env"] = "GQT_GODOT_BIN" in os.environ
            return {"tool": "coverage", "ok": True, "summary": {},
                    "failures": []}

        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        monkeypatch.setattr(cov_runner, "run_coverage", fake_run_coverage)
        rc = main(["coverage", str(target), "--project-root", str(tmp_path)])
        assert rc == 0
        assert captured["had_env"] is False


class TestFixRoundF1F2:
    """修复轮 F-1/F-2（hardener 上报，Leader 复现确认）回归钉子。

    F-1：coverage / accept run|mutate 的显式二进制配置错误（env 或 flag 非法
    路径）必须走统一 JSON 错误契约（ok:false + summary.error + failures），且
    先于任何其他校验（GUT runner 缺失不得掩盖二进制配置错误）。
    F-2：命令级覆盖注入后 binary kind 必须重新判定——coverage / accept 不得
    沿用同进程旧缓存；--path 形态与 timing 键成分取覆盖后真实类型。
    """

    def _run_main(self, argv, capsys):
        from godot_qa_toolkit.cli import main
        rc = main(argv)
        out = capsys.readouterr().out
        return rc, json.loads(out)

    def _mk_target(self, tmp_path: Path) -> Path:
        target = tmp_path / "f.gd"
        target.write_text("func a():\n\tpass\n", encoding="utf-8")
        return target

    def test_coverage_invalid_env_contract_not_traceback(self, tmp_path,
                                                         capsys, monkeypatch):
        monkeypatch.setenv("GQT_GODOT_BIN", "/no/such")
        target = self._mk_target(tmp_path)
        rc, payload = self._run_main(
            ["coverage", str(target), "--project-root", str(tmp_path)], capsys)
        assert rc == 1
        assert payload["tool"] == "coverage"
        assert payload["ok"] is False
        assert "GQT_GODOT_BIN" in payload["summary"]["error"]
        assert "GQT_GODOT_BIN" in payload["failures"][0]["reason"]

    def test_accept_run_invalid_env_contract(self, tmp_path, capsys,
                                             monkeypatch):
        monkeypatch.setenv("GQT_GODOT_BIN", "/no/such")
        rc, payload = self._run_main(
            ["accept", "run", "--project-root", str(tmp_path), "--key", "k"],
            capsys)
        assert rc == 1
        assert payload["tool"] == "accept"
        assert payload["ok"] is False
        assert "GQT_GODOT_BIN" in payload["summary"]["error"]

    def test_accept_mutate_invalid_env_contract(self, tmp_path, capsys,
                                                monkeypatch):
        monkeypatch.setenv("GQT_GODOT_BIN", "/no/such")
        rc, payload = self._run_main(
            ["accept", "mutate", "--project-root", str(tmp_path), "--key", "k"],
            capsys)
        assert rc == 1
        assert payload["tool"] == "accept"
        assert payload["ok"] is False
        assert "GQT_GODOT_BIN" in payload["summary"]["error"]

    def test_accept_binary_config_precedes_gut_check(self, tmp_path, capsys,
                                                     monkeypatch):
        # 项目根无 addons/gut：若 GUT 校验先行会以 "GUT runner not found"
        # 掩盖二进制配置错误——二进制契约必须先行。
        monkeypatch.setenv("GQT_GODOT_BIN", "/no/such")
        rc, payload = self._run_main(
            ["accept", "run", "--project-root", str(tmp_path), "--key", "k"],
            capsys)
        assert "GQT_GODOT_BIN" in payload["summary"]["error"]
        assert "GUT runner" not in payload["summary"]["error"]

    def test_mutation_invalid_env_contract_unchanged(self, tmp_path, capsys,
                                                     monkeypatch):
        # 行为不退化钉子：mutation env 通道契约形态保持（修复轮重构不得动它）。
        monkeypatch.setenv("GQT_GODOT_BIN", "/no/such")
        target = self._mk_target(tmp_path)
        rc, payload = self._run_main(
            ["mutation", str(target), "--project-root", str(tmp_path)], capsys)
        assert rc == 1
        assert payload["tool"] == "mutation"
        assert payload["ok"] is False
        assert "GQT_GODOT_BIN" in payload["summary"]["error"]

    def test_kind_repinned_after_flag_switch_same_process(self, tmp_path,
                                                          monkeypatch):
        elf = _mk_binary(tmp_path, "godot_lin")
        exe = _mk_binary(tmp_path, "Godot_w64.exe", b"MZ\x90\x00")
        monkeypatch.setattr(inject, "wslpath_win", lambda p: "D:\\proj")
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        assert inject.detect_binary_kind() == "linux"
        from godot_qa_toolkit.cli import _apply_godot_bin_or_contract
        ns = SimpleNamespace(godot_bin=str(exe))
        assert _apply_godot_bin_or_contract("coverage", ns) is None
        # F-2 主断言：无手动 reset，缓存已随覆盖重判
        assert inject.detect_binary_kind() == "win64"
        assert inject.godot_project_path("/mnt/d/proj") == "D:\\proj"

    def test_kind_repinned_after_env_switch_same_process(self, tmp_path,
                                                         monkeypatch):
        elf = _mk_binary(tmp_path, "godot_lin")
        exe = _mk_binary(tmp_path, "Godot_w64.exe", b"MZ\x90\x00")
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        assert inject.detect_binary_kind() == "linux"
        monkeypatch.setenv("GQT_GODOT_BIN", str(exe))
        from godot_qa_toolkit.cli import _apply_godot_bin_or_contract
        assert _apply_godot_bin_or_contract(
            "accept", SimpleNamespace(godot_bin=None)) is None
        assert inject.detect_binary_kind() == "win64"

    def test_cli_coverage_flag_switch_repins_kind(self, tmp_path, capsys,
                                                  monkeypatch):
        elf = _mk_binary(tmp_path, "godot_lin")
        exe = _mk_binary(tmp_path, "Godot_w64.exe", b"MZ\x90\x00")
        target = self._mk_target(tmp_path)
        captured: dict = {}

        def fake_run_coverage(**kwargs):
            captured["kind"] = inject.detect_binary_kind()
            return {"tool": "coverage", "ok": True, "summary": {},
                    "failures": []}

        monkeypatch.setattr(cov_runner, "run_coverage", fake_run_coverage)
        rc1, _ = self._run_main(
            ["coverage", str(target), "--project-root", str(tmp_path),
             "--godot-bin", str(elf)], capsys)
        rc2, _ = self._run_main(
            ["coverage", str(target), "--project-root", str(tmp_path),
             "--godot-bin", str(exe)], capsys)
        assert rc1 == 0 and rc2 == 0
        assert captured["kind"] == "win64"

    def test_unset_env_dispatch_proceeds_after_pin(self, tmp_path, capsys,
                                                   monkeypatch):
        # 冻结面（§SPEC-079 B 恢复）：unset + 无 flag 时重判不抛错，派发
        # 照常（PATH 空解析兜底）——启动校验延迟到真实拉起点
        # （spawn_godot），mock 生效前不再静态拦截。
        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        inject.reset_binary_kind_cache()
        target = self._mk_target(tmp_path)
        captured: dict = {}

        def fake_run_coverage(**kwargs):
            captured["called"] = True
            return {"tool": "coverage", "ok": True, "summary": {},
                    "failures": []}

        monkeypatch.setattr(cov_runner, "run_coverage", fake_run_coverage)
        assert inject.detect_binary_kind() == "win64"
        rc, payload = self._run_main(
            ["coverage", str(target), "--project-root", str(tmp_path)], capsys)
        assert rc == 0
        assert captured["called"] is True
