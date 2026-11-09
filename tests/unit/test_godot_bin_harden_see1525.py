"""SEE-1525 hardener：统一 Godot 解析链的判别力补强（对抗性硬化）。

既有守护测试 `test_godot_bin_unify_see1525.py`（29 例）+ 平台冻结钉子
（`test_inject_platform_see1405.py` / `test_argv_samesource_see1405.py` /
`test_hardener_1312.py`）覆盖了主干路径，但以下反例在变异注入下**存活**
（未被任何既有断言击杀）。本文件为每个存活面补上可机器判定的钉子：

- PATH 通道：首个匹配优先 / 不可执行文件与同名目录不得被选中 / PATH 空段
  不得让 cwd 里的 godot 劫持解析
- env 通道：显式路径的「不存在 / 不可读 / 不可执行」三类校验与来源标签、
  值去空白——（flag 通道同类校验已有钉子，env 通道此前无）
- override 通道：空串 flag 的错误消息必须点明「非空路径」要求（不是
  退化成的「路径不存在」）；未传 flag 时 os.environ 必须逐字节不变
- --path 形态：win64 下非 /mnt/ 的绝对路径不得被 wslpath 转换（既有断言
  依赖真实 wslpath，在非 WSL CI 上退化为空断言）
- 二进制类型：wrapper exec 目标（普通 / 带引号两种写法）必须递归判定；
  detect_binary_kind 的 per-process 缓存语义（无参读缓存 / refresh=True
  重新判定）
- env 注入通道：win64 的 XDG_DATA_HOME / APPDATA 必须走 _win_path 转换；
  WSLENV 必须追加而非覆写调用方既有值；linux 通道注入键集恰为两项
- wslpath 原语：转换失败/超时必须返回 None（docstring 契约）
- 启动点接线：mutation runner 的 --path 必须经 _godot_project_path

所有断言均为机器可判定（精确值 / 精确键集 / 精确异常类型与消息），
不弱化既有断言面；不改任何行为面代码。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from godot_qa_toolkit.coverage import runner as cov_runner
from godot_qa_toolkit.mutation import inject
from godot_qa_toolkit.mutation import runner as mrunner

ELF_MAGIC = b"\x7fELF" + b"\x00" * 8
WIN64_MAGIC = b"MZ\x90\x00"

# win64 通道注入的 WSLENV 条目（与 inject._win_env 契约逐字一致）
WIN64_WSLENV_ENTRIES = "GQT_MUTATION_CFG/w:XDG_DATA_HOME/w:APPDATA/w"


def _mk_binary(tmp_path: Path, name: str, content: bytes = ELF_MAGIC) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    p.chmod(0o755)
    return p


@pytest.fixture(autouse=True)
def _fresh_kind_cache():
    """per-process 二进制类型缓存按用例清空——env 变更必须反映到判定。"""
    inject.reset_binary_kind_cache()
    yield
    inject.reset_binary_kind_cache()


@pytest.fixture(autouse=True)
def _env_guard(monkeypatch):
    """GQT_GODOT_BIN 在本机 ~/.bashrc 全局导出——按用例显式设定/清除，
    防跨用例泄漏（各用例自带 delenv/setenv，此处只兜底 unset 面）。"""
    monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
    monkeypatch.delenv("WSLENV", raising=False)
    yield


class TestPathChannelResolution:
    """PATH 通道反例：首个匹配优先 + 候选资格（可执行 + 常规文件）+ 空段。"""

    def test_first_match_wins_over_later_candidate(self, tmp_path,
                                                   monkeypatch):
        """两个 PATH 目录各有一个 godot → 必须取**首个**匹配（不是末个）。"""
        first, second = tmp_path / "a_first", tmp_path / "z_second"
        first.mkdir()
        second.mkdir()
        _mk_binary(first, "godot")
        _mk_binary(second, "godot")
        monkeypatch.setenv("PATH", f"{first}{os.pathsep}{second}")
        assert inject.godot_command() == str(first / "godot")

    def test_non_executable_file_is_not_selected(self, tmp_path, monkeypatch):
        """PATH 上的不可执行常规文件不是二进制候选（不得被选中）。"""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        cand = bindir / "godot"
        cand.write_bytes(ELF_MAGIC)
        cand.chmod(0o644)
        monkeypatch.setenv("PATH", str(bindir))
        assert inject.godot_command() == "godot"
        assert inject._resolved_godot_path() is None

    def test_directory_named_godot_is_not_selected(self, tmp_path, monkeypatch):
        """PATH 上的同名**目录**不是二进制候选（目录默认可 X_OK——必须拒）。"""
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "godot").mkdir()
        monkeypatch.setenv("PATH", str(bindir))
        assert inject.godot_command() == "godot"
        assert inject._resolved_godot_path() is None

    def test_empty_path_segment_does_not_resolve_cwd_godot(self, tmp_path,
                                                          monkeypatch):
        """PATH 空段（POSIX 语义=当前目录）不得让 cwd 里的 godot 劫持解析：
        空段必须被跳过 → 解析失败回退字面量，类型判定维持 win64 缺省。"""
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        _mk_binary(cwd, "godot")
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("PATH", "")
        assert inject._resolved_godot_path() is None
        assert inject.godot_command() == "godot"
        assert inject.detect_binary_kind() == "win64"


class TestExplicitEnvChannel:
    """env 通道反例：显式路径的三类校验 + 来源标签 + 值去空白。

    flag 通道的同类校验已有钉子（test_godot_bin_unify_see1525.py），env
    通道此前**无任何断言**——静默回退会把配置错误藏进 verdict。
    """

    def test_nonexistent_path_raises_with_env_label(self, tmp_path,
                                                    monkeypatch):
        monkeypatch.setenv("GQT_GODOT_BIN", str(tmp_path / "no_such"))
        with pytest.raises(inject.BinaryConfigError,
                           match=r"GQT_GODOT_BIN path does not exist"):
            inject._resolved_godot_path()

    def test_non_executable_path_raises_with_env_label(self, tmp_path,
                                                       monkeypatch):
        p = tmp_path / "godot"
        p.write_bytes(ELF_MAGIC)
        p.chmod(0o644)
        monkeypatch.setenv("GQT_GODOT_BIN", str(p))
        with pytest.raises(inject.BinaryConfigError,
                           match=r"GQT_GODOT_BIN path is not executable"):
            inject._resolved_godot_path()

    def test_unreadable_path_raises_with_env_label(self, tmp_path,
                                                   monkeypatch):
        p = tmp_path / "godot"
        p.write_bytes(ELF_MAGIC)
        p.chmod(0o000)
        monkeypatch.setenv("GQT_GODOT_BIN", str(p))
        with pytest.raises(inject.BinaryConfigError,
                           match=r"GQT_GODOT_BIN path is not readable"):
            inject._resolved_godot_path()

    def test_env_value_is_stripped(self, tmp_path, monkeypatch):
        """显式 env 值两端的空白必须被剥掉（否则解析成不存在的路径）。"""
        elf = _mk_binary(tmp_path, "godot_lin")
        monkeypatch.setenv("GQT_GODOT_BIN", f"  {elf}  ")
        assert inject.godot_command() == str(elf)
        assert inject.detect_binary_kind() == "linux"


class TestOverrideChannel:
    """--godot-bin override 通道反例：错误消息精确化 + unset 面零动作。"""

    def test_blank_flag_reports_non_empty_requirement(self):
        """空串 flag 的错误必须点明「非空路径」要求——不得退化成
        「路径不存在」（后者是另一个契约）。"""
        with pytest.raises(inject.BinaryConfigError, match="non-empty path"):
            inject.apply_godot_bin_override("   ")

    def test_flag_error_label_is_godot_bin(self, tmp_path):
        with pytest.raises(inject.BinaryConfigError,
                           match=r"^--godot-bin path does not exist"):
            inject.apply_godot_bin_override(str(tmp_path / "no_such"))

    def test_absent_flag_leaves_environ_untouched(self):
        """未传 flag → os.environ 逐字节不变（冻结红线：unset 面零动作，
        不得把解析结果回写 env 泄漏给子进程）。"""
        before = dict(os.environ)
        inject.apply_godot_bin_override(None)
        assert dict(os.environ) == before


class TestPathFormHardening:
    """--path 形态反例：win64 只转换 /mnt/ 前缀（与 wslpath 可用性无关）。"""

    def test_non_mnt_absolute_path_not_converted_under_win64(self,
                                                              monkeypatch):
        """win64 下非 /mnt/ 的绝对路径必须原样（不得被 wslpath 转换）。
        既有断言依赖真实 wslpath（无 wslpath 的 CI 上退化为空断言）——
        此处改用 mock，判别与环境无关。"""
        seen: list[str] = []
        monkeypatch.setattr(inject, "wslpath_win",
                            lambda p: seen.append(p) or "D:\\x")
        assert inject.godot_project_path("/home/x/proj",
                                         kind="win64") == "/home/x/proj"
        assert seen == []

    def test_mnt_path_is_converted_under_win64(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(inject, "wslpath_win",
                            lambda p: seen.append(p) or "D:\\x")
        assert inject.godot_project_path("/mnt/d/proj",
                                         kind="win64") == "D:\\x"
        assert seen == ["/mnt/d/proj"]

    def test_linux_kind_never_converts(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(inject, "wslpath_win",
                            lambda p: seen.append(p) or "D:\\x")
        assert inject.godot_project_path("/mnt/d/proj",
                                         kind="linux") == "/mnt/d/proj"
        assert seen == []


class TestBinaryKindHardening:
    """二进制类型判定反例：wrapper exec 目标递归 + per-process 缓存语义。"""

    def test_wrapper_execing_linux_elf_detected_as_linux(self, tmp_path,
                                                          monkeypatch):
        """bash wrapper 的 exec 目标必须递归判定（exec Linux ELF → linux）。"""
        elf = _mk_binary(tmp_path, "godot_real")
        wrapper = tmp_path / "godot"
        wrapper.write_text(f"#!/bin/bash\nexec {elf} \"$@\"\n",
                           encoding="utf-8")
        wrapper.chmod(0o755)
        monkeypatch.setenv("GQT_GODOT_BIN", str(wrapper))
        assert inject.detect_binary_kind() == "linux"

    def test_wrapper_quoted_exec_target_detected(self, tmp_path, monkeypatch):
        """带引号的 exec 目标（exec "<path>"）同样必须被解析。"""
        elf = _mk_binary(tmp_path, "godot_real")
        wrapper = tmp_path / "godot"
        wrapper.write_text(f"#!/bin/bash\nexec \"{elf}\" \"$@\"\n",
                           encoding="utf-8")
        wrapper.chmod(0o755)
        monkeypatch.setenv("GQT_GODOT_BIN", str(wrapper))
        assert inject.detect_binary_kind() == "linux"

    def test_detect_without_refresh_returns_cached_kind(self, tmp_path,
                                                        monkeypatch):
        """无参 detect 读 per-process 缓存：env 变更后仍返回首次判定。"""
        elf = _mk_binary(tmp_path, "godot_lin")
        exe = _mk_binary(tmp_path, "Godot_win64.exe", WIN64_MAGIC)
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        assert inject.detect_binary_kind() == "linux"
        monkeypatch.setenv("GQT_GODOT_BIN", str(exe))
        assert inject.detect_binary_kind() == "linux"

    def test_detect_with_refresh_redetects_after_env_change(self, tmp_path,
                                                            monkeypatch):
        """refresh=True 必须绕过缓存重新判定（--godot-bin 覆盖后的关键路径）。"""
        elf = _mk_binary(tmp_path, "godot_lin")
        exe = _mk_binary(tmp_path, "Godot_win64.exe", WIN64_MAGIC)
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        assert inject.detect_binary_kind() == "linux"
        monkeypatch.setenv("GQT_GODOT_BIN", str(exe))
        assert inject.detect_binary_kind(refresh=True) == "win64"


class TestEnvInjectionChannelHardening:
    """子进程 env 注入通道反例：win64 转换面 / WSLENV 追加 / linux 键集。"""

    def _win64_env(self, tmp_path, monkeypatch) -> dict:
        exe = _mk_binary(tmp_path, "godot.exe", WIN64_MAGIC)
        monkeypatch.setenv("GQT_GODOT_BIN", str(exe))
        monkeypatch.setattr(inject, "wslpath_win", lambda p: "D:\\" + p)
        # SEE-1550：mutant_env(cfg_fs, user_dir)——cfg/user 双值由调用方提供
        env, _ = inject.mutant_env(str(tmp_path / "cfg.json"),
                                   str(tmp_path / "user"))
        return env

    def test_win64_xdg_and_appdata_are_converted(self, tmp_path, monkeypatch):
        """win64 通道的 XDG_DATA_HOME / APPDATA 必须经 _win_path 转 Windows
        形式（原生路径给 win64 引擎读不到）。"""
        env = self._win64_env(tmp_path, monkeypatch)
        assert env["XDG_DATA_HOME"].startswith("D:\\")
        assert env["APPDATA"].startswith("D:\\")

    def test_win64_wslenv_appends_to_existing(self, tmp_path, monkeypatch):
        """调用方既有 WSLENV 必须保留并追加（不得覆写，LOW-4）。"""
        monkeypatch.setenv("WSLENV", "FOO/w:BAR/w:")
        env = self._win64_env(tmp_path, monkeypatch)
        assert env["WSLENV"] == f"FOO/w:BAR/w:{WIN64_WSLENV_ENTRIES}"

    def test_win64_wslenv_without_existing_is_exact(self, tmp_path,
                                                    monkeypatch):
        env = self._win64_env(tmp_path, monkeypatch)
        assert env["WSLENV"] == WIN64_WSLENV_ENTRIES

    def test_linux_channel_key_set_is_exactly_two(self, tmp_path, monkeypatch):
        """linux 通道注入键集恰为两项（APPDATA / WSLENV 属 win64-only 语义）。"""
        elf = _mk_binary(tmp_path, "godot_lin")
        monkeypatch.setenv("GQT_GODOT_BIN", str(elf))
        env, _ = inject.mutant_env(str(tmp_path / "cfg.json"),
                                   str(tmp_path / "user"))
        assert set(env) == {"XDG_DATA_HOME", "GQT_MUTATION_CFG"}


class TestWslpathPrimitive:
    """wslpath 原语反例：失败/超时必须返回 None（docstring 契约）。"""

    def test_returns_none_when_conversion_fails(self, monkeypatch):
        """退出码非 0 → None。"""
        monkeypatch.setattr(inject.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(returncode=1,
                                                           stdout=""))
        assert inject.wslpath_win("/mnt/d/x") is None

    def test_returns_none_when_stdout_empty(self, monkeypatch):
        """退出码 0 但 stdout 为空/纯空白 → None（不得返回空串——空串在
        `wslpath_win(p) or p` 兜底链上被掩掉，但直接调用方会拿到非 None）。"""
        monkeypatch.setattr(inject.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(returncode=0,
                                                           stdout="  \n"))
        assert inject.wslpath_win("/mnt/d/x") is None

    def test_returns_none_on_timeout(self, monkeypatch):
        class _FakeSubprocess:
            TimeoutExpired = subprocess.TimeoutExpired

            @staticmethod
            def run(*a, **k):
                raise subprocess.TimeoutExpired(cmd="wslpath", timeout=10)

        monkeypatch.setattr(inject, "subprocess", _FakeSubprocess)
        assert inject.wslpath_win("/mnt/d/x") is None


class TestLaunchPointWiring:
    """启动点接线反例：--path 实参必须经 _godot_project_path 统一原语。"""

    def test_mutation_runner_path_argument_uses_helper(self, tmp_path,
                                                       monkeypatch):
        root = tmp_path / "proj"
        (root / "addons" / "gut").mkdir(parents=True)
        (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
        monkeypatch.setattr(mrunner, "_godot_project_path",
                            lambda p: "WIN:" + p)
        monkeypatch.setattr(mrunner, "GlobalGodotGate",
                            contextlib.nullcontext)
        seen: dict = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = list(argv)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(mrunner.subprocess, "run", fake_run)
        mrunner._run_gut_on_project(str(root), timeout_s=5)
        argv = seen["argv"]
        assert argv[argv.index("--path") + 1] == "WIN:" + str(root)


class TestCliContractHardening:
    """CLI 契约反例：优先级在 CLI 面可见 + env 通道错误仍是 JSON 契约。"""

    def _run_main(self, argv, capsys):
        from godot_qa_toolkit.cli import main
        rc = main(argv)
        out = capsys.readouterr().out
        return rc, json.loads(out)

    def test_cli_flag_overrides_env_at_cli_level(self, tmp_path, capsys,
                                                 monkeypatch):
        """flag > env 的优先级必须在 CLI 面成立（env 设了也不能夺权）。"""
        env_bin = _mk_binary(tmp_path, "env_bin")
        flag_bin = _mk_binary(tmp_path, "flag_bin")
        monkeypatch.setenv("GQT_GODOT_BIN", str(env_bin))
        captured: dict = {}

        def fake_run_coverage(**kwargs):
            captured["env_bin"] = os.environ.get("GQT_GODOT_BIN")
            return {"tool": "coverage", "ok": True, "summary": {},
                    "failures": []}

        monkeypatch.setattr(cov_runner, "run_coverage", fake_run_coverage)
        target = tmp_path / "f.gd"
        target.write_text("func a():\n\tpass\n", encoding="utf-8")
        rc, payload = self._run_main(
            ["coverage", str(target), "--project-root", str(tmp_path),
             "--godot-bin", str(flag_bin)], capsys)
        assert rc == 0
        assert payload["ok"] is True
        assert captured["env_bin"] == str(flag_bin)

    def test_cli_mutation_redetects_kind_after_flag_override(self, tmp_path,
                                                             capsys,
                                                             monkeypatch):
        """mutation dispatch 必须在 --godot-bin 覆盖后**重新判定**二进制类型
        （detect(refresh=True)）——同进程内二次调用换了二进制时，类型不得
        沿用上一次的缓存。"""
        from godot_qa_toolkit import cli
        from godot_qa_toolkit.cli import main

        linux = _mk_binary(tmp_path, "godot_linux")
        win64 = _mk_binary(tmp_path, "Godot_win64.exe", WIN64_MAGIC)
        seen: list[str] = []

        def fake_dispatch(args, binary_kind):
            seen.append(binary_kind)
            return {"tool": "mutation", "ok": True, "summary": {},
                    "failures": []}

        monkeypatch.setattr(cli, "_dispatch_mutation", fake_dispatch)
        target = tmp_path / "f.gd"
        target.write_text("func a():\n\tpass\n", encoding="utf-8")
        for binary in (linux, win64):
            rc = main(["mutation", str(target), "--project-root",
                       str(tmp_path), "--godot-bin", str(binary)])
            capsys.readouterr()
            assert rc == 0
        assert seen == ["linux", "win64"]

    def test_mutation_bad_env_path_yields_json_contract(self, tmp_path,
                                                        capsys, monkeypatch):
        """env 通道显式路径非法 → 统一 JSON 错误契约（不得裸抛 traceback）。"""
        monkeypatch.setenv("GQT_GODOT_BIN", str(tmp_path / "no_such"))
        target = tmp_path / "f.gd"
        target.write_text("func a():\n\tpass\n", encoding="utf-8")
        rc, payload = self._run_main(
            ["mutation", str(target), "--project-root", str(tmp_path)],
            capsys)
        assert rc == 1
        assert payload["tool"] == "mutation"
        assert payload["ok"] is False
        assert "GQT_GODOT_BIN" in payload["summary"]["error"]
        assert payload["failures"]

    def test_unset_env_child_process_env_has_no_godot_bin(self, tmp_path,
                                                          monkeypatch):
        """unset 面：coverage 子进程 env 不得出现 GQT_GODOT_BIN 注入键
        （§SPEC-1525-006 新增键冻结）。"""
        root = tmp_path / "proj"
        (root / "addons" / "gut").mkdir(parents=True)
        (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
        target = root / "calc.gd"
        target.write_text("func add(a, b):\n\treturn a + b\n",
                          encoding="utf-8")
        monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setattr("godot_qa_toolkit.mutation.godot_gate."
                            "GlobalGodotGate", contextlib.nullcontext)
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["env_bin"] = os.environ.get("GQT_GODOT_BIN")
            captured["kwargs"] = kwargs
            return SimpleNamespace(returncode=0, stdout="* test_ok", stderr="")

        monkeypatch.setattr(cov_runner.subprocess, "run", fake_run)
        outcome = cov_runner._run_gut_for_coverage(
            target, str(root), "original", "instrumented", 3, 80.0, 60)
        assert outcome is None
        assert captured["env_bin"] is None
        assert "env" not in captured["kwargs"]
