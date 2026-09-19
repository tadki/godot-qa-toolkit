"""SEE-1319 缺陷2：gqt doctor 安装路径自检单元测试。

doctor 比对 editable 安装位置与当前工作树 .dev/qa-toolkit 的 gitlink HEAD，
不一致即 exit 2 + stderr 修复指引——跨工作树行为不一致（coverage 探针版本
错配）的治理入口。
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "src"))

from godot_qa_toolkit.doctor import (
    check_install,
    locate_toolkit_root,
    resolve_editable_location,
)


class TestResolveEditableLocation:
    def test_returns_path_when_editable_installed(self):
        # 本工作机存在 editable 安装（指向旧工作树）——只要求能解析出路径。
        loc = resolve_editable_location()
        assert loc is None or Path(loc).is_dir()

    def test_unresolvable_returns_none_not_crash(self, monkeypatch):
        # pip 不可用/未安装：失败兜底返回 None，绝不抛异常（WSL 稳健性）。
        monkeypatch.setattr(
            "godot_qa_toolkit.doctor._pip_show",
            lambda: None,
        )
        assert resolve_editable_location() is None


class TestLocateToolkitRoot:
    def test_walks_up_to_dev_qa_toolkit(self, tmp_path):
        root = tmp_path / "wt" / "KingOfLikes-Godot" / ".dev" / "qa-toolkit"
        root.mkdir(parents=True)
        cwd = root / "src" / "godot_qa_toolkit"
        cwd.mkdir(parents=True)
        assert locate_toolkit_root(cwd) == root

    def test_no_root_found_returns_none(self, tmp_path):
        assert locate_toolkit_root(tmp_path) is None


class TestCheckInstall:
    def _fake_env(self, tmp_path, monkeypatch, editable, git_head):
        root = tmp_path / "wt" / "KingOfLikes-Godot" / ".dev" / "qa-toolkit"
        root.mkdir(parents=True)
        monkeypatch.setattr(
            "godot_qa_toolkit.doctor.resolve_editable_location",
            lambda: editable,
        )
        monkeypatch.setattr(
            "godot_qa_toolkit.doctor._gitlink_head",
            lambda root_path: git_head,
        )
        return root

    def test_match_returns_ok(self, tmp_path, monkeypatch):
        root = self._fake_env(tmp_path, monkeypatch,
                              editable=str(tmp_path / "wt" / "KingOfLikes-Godot" / ".dev" / "qa-toolkit"),
                              git_head="abc123")
        # editable 与 gitlink 根一致即视为已对齐（HEAD 比对在 real env 由调用方
        # 传入 _gitlink_head 的 mock 决定，这里 mock 返回固定值 → root 匹配即可）。
        ok, detail = check_install(cwd=root / "src")
        assert ok is True
        assert detail["status"] == "ok"

    def test_stale_editable_exits_mismatch(self, tmp_path, monkeypatch):
        stale = tmp_path / "other" / "see-1317" / "qa-toolkit"
        stale.mkdir(parents=True)
        root = self._fake_env(tmp_path, monkeypatch,
                              editable=str(stale), git_head="abc123")
        ok, detail = check_install(cwd=root / "src")
        assert ok is False
        assert detail["status"] == "mismatch"
        assert "install.sh" in detail["fix_hint"]

    def test_missing_editable_reports_not_installed(self, tmp_path, monkeypatch):
        root = self._fake_env(tmp_path, monkeypatch,
                              editable=None, git_head="abc123")
        ok, detail = check_install(cwd=root / "src")
        assert ok is False
        assert detail["status"] == "not_installed"

    def test_missing_toolkit_root_is_neutral(self, tmp_path, monkeypatch):
        # cwd 不在任何 qa-toolkit 内（如主仓外随意目录）——自检无从比对，
        # 报 ok/skipped 而非误报错。
        monkeypatch.setattr(
            "godot_qa_toolkit.doctor.resolve_editable_location",
            lambda: None,
        )
        ok, detail = check_install(cwd=tmp_path)
        assert ok is True
        assert detail["status"] == "skipped"


class TestDoctorCli:
    def _run_cli(self, *argv, cwd):
        proc = subprocess.run(
            [sys.executable, "-m", "godot_qa_toolkit.cli", *argv],
            capture_output=True, text=True, cwd=str(cwd),
            env=None,
        )
        return proc

    def test_doctor_subcommand_registered(self):
        from godot_qa_toolkit.cli import build_parser
        args = build_parser().parse_args(["doctor"])
        assert args.func is not None


class TestDoctorHardening:
    """SEE-1319 hardener：doctor 边界对抗补强（默认 doctor 是错的）。"""

    def test_gitlink_head_read_failure_degrades_to_none_not_crash(self, tmp_path, monkeypatch):
        # 非 git 目录 / git 命令失败 → gitlink_head=None，不抛异常。
        from godot_qa_toolkit import doctor
        assert doctor._gitlink_head(tmp_path) is None

    def test_gitlink_head_timeout_returns_none(self, tmp_path, monkeypatch):
        import subprocess
        from godot_qa_toolkit import doctor

        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="git", timeout=10)

        monkeypatch.setattr(doctor.subprocess, "run", boom)
        assert doctor._gitlink_head(tmp_path) is None

    def test_mismatch_detail_carries_both_locations(self, tmp_path, monkeypatch):
        # 错配详情必须同时暴露 editable 与 toolkit_root——修复指引的机器判据。
        from godot_qa_toolkit import doctor
        root = tmp_path / "wt" / ".dev" / "qa-toolkit"
        root.mkdir(parents=True)
        stale = tmp_path / "old" / "qa-toolkit"
        stale.mkdir(parents=True)
        monkeypatch.setattr(doctor, "resolve_editable_location", lambda: str(stale))
        monkeypatch.setattr(doctor, "_gitlink_head", lambda r: "deadbeef")
        ok, detail = doctor.check_install(cwd=root / "src")
        assert ok is False
        assert detail["editable_location"] == str(stale)
        assert detail["toolkit_root"] == str(root)
        assert detail["gitlink_head"] == "deadbeef"

    def test_editable_missing_but_toolkit_present_is_not_installed(self, tmp_path, monkeypatch):
        # 有工作树、无 editable 安装 → not_installed（非 skipped——用户须装）。
        from godot_qa_toolkit import doctor
        root = tmp_path / "wt" / ".dev" / "qa-toolkit"
        root.mkdir(parents=True)
        monkeypatch.setattr(doctor, "resolve_editable_location", lambda: None)
        ok, detail = doctor.check_install(cwd=root / "src")
        assert ok is False
        assert detail["status"] == "not_installed"

    def test_editable_present_without_toolkit_root_is_skipped(self, tmp_path, monkeypatch):
        # 有安装但 cwd 不在任何工具树内（如主仓外裸跑 gqt）→ 无法判定目标树，跳过。
        from godot_qa_toolkit import doctor
        monkeypatch.setattr(doctor, "resolve_editable_location", lambda: str(tmp_path))
        ok, detail = doctor.check_install(cwd=tmp_path)
        assert ok is True
        assert detail["status"] == "skipped"

    def test_pip_show_stderr_noise_does_not_break_parsing(self, tmp_path, monkeypatch):
        # pip 输出夹带 warning 行仍能解析 Editable project location。
        from godot_qa_toolkit import doctor

        def noisy_show():
            return ("WARNING: pip is old\n"
                    "Name: godot-qa-toolkit\n"
                    "Editable project location: /some/path\n"
                    "Location: /home/jerry/.local/lib/python3.12/site-packages\n")

        monkeypatch.setattr(doctor, "_pip_show", noisy_show)
        assert doctor.resolve_editable_location() == "/some/path"

    def test_pip_show_empty_editable_location_yields_none(self, monkeypatch):
        # 'Editable project location:' 值为空（非 editable 装法）→ None。
        from godot_qa_toolkit import doctor

        def show():
            return "Name: godot-qa-toolkit\nEditable project location: \n"

        monkeypatch.setattr(doctor, "_pip_show", show)
        assert doctor.resolve_editable_location() is None

    def test_relative_vs_absolute_same_root_matches(self, tmp_path, monkeypatch):
        # editable 是绝对路径、toolkit_root 解析自 walk-up——resolve() 比对必须
        # 消掉 symlink/../ 差异；用同一真实路径两种表达验证不误报。
        from godot_qa_toolkit import doctor
        root = tmp_path / "wt" / ".dev" / "qa-toolkit"
        root.mkdir(parents=True)
        monkeypatch.setattr(doctor, "resolve_editable_location",
                            lambda: str(root))  # 无尾斜杠、无 ../——最简形态
        monkeypatch.setattr(doctor, "_gitlink_head", lambda r: "abc")
        ok, detail = doctor.check_install(cwd=root / "src" / "godot_qa_toolkit")
        assert ok is True
        assert detail["status"] == "ok"


class TestInstallShContract:
    """scripts/install.sh 契约（pytest 内 stub 验证，不真装 pip）。"""

    REPO = Path(__file__).parent.parent.parent
    SCRIPT = REPO / "scripts" / "install.sh"

    def test_script_exists_executable_and_syntax_ok(self):
        import subprocess
        assert self.SCRIPT.is_file()
        assert self.SCRIPT.stat().st_mode & 0o111, "install.sh 必须可执行"
        r = subprocess.run(["bash", "-n", str(self.SCRIPT)], capture_output=True)
        assert r.returncode == 0, r.stderr.decode()

    def test_script_refuses_non_toolkit_location(self, tmp_path):
        # 把脚本拷到非工具树位置执行 → 必须报错退出（自检哨兵）。
        import subprocess
        fake = tmp_path / "not-a-toolkit"
        fake.mkdir()
        (fake / "scripts").mkdir()
        (fake / "scripts" / "install.sh").write_text(self.SCRIPT.read_text())
        r = subprocess.run(["bash", str(fake / "scripts" / "install.sh")],
                           capture_output=True, text=True)
        assert r.returncode != 0
        assert "not a godot-qa-toolkit checkout" in r.stderr

    def test_script_reentrant_run_in_real_tooltree(self, tmp_path):
        # 真实工具树拷贝 + stub python3/pip（绕开真实安装）→ 全流程可重入成功。
        import subprocess
        stub_dir = tmp_path / "stubbin"
        stub_dir.mkdir()
        (stub_dir / "python3").write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = '-c' ]; then eval \"$2\"; exit 0; fi\n"
            "if [ \"$2\" = '-e' ]; then exit 0; fi\n"  # pip install -e stub
            "exit 0\n")
        (stub_dir / "python3").chmod(0o755)
        tree = tmp_path / "wt" / ".dev" / "qa-toolkit"
        (tree / "src" / "godot_qa_toolkit").mkdir(parents=True)
        (tree / "scripts").mkdir()
        (tree / "pyproject.toml").write_text("[project]\n")
        (tree / "src" / "godot_qa_toolkit" / "__init__.py").write_text("")
        shutil.copy2(self.SCRIPT, tree / "scripts" / "install.sh")
        env = {"PATH": f"{stub_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)}
        r = subprocess.run(["bash", str(tree / "scripts" / "install.sh")],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        assert "done" in r.stdout


class TestInstallShPep668:
    """SEE-1319 D1：install.sh PEP 668 降级路径（stub pip 首拒 + 重试成功）。"""

    REPO = Path(__file__).parent.parent.parent
    SCRIPT = REPO / "scripts" / "install.sh"

    def _make_tree(self, tmp_path):
        stub_dir = tmp_path / "stubbin"
        stub_dir.mkdir()
        tree = tmp_path / "wt" / ".dev" / "qa-toolkit"
        (tree / "src" / "godot_qa_toolkit").mkdir(parents=True)
        (tree / "scripts").mkdir()
        (tree / "pyproject.toml").write_text("[project]\n")
        (tree / "src" / "godot_qa_toolkit" / "__init__.py").write_text("")
        shutil.copy2(self.SCRIPT, tree / "scripts" / "install.sh")
        return stub_dir, tree

    def _write_stub_pip(self, stub_dir, script_body):
        stub = stub_dir / "python3"
        stub.write_text(script_body)
        stub.chmod(0o755)

    def test_pep668_rejection_triggers_flagged_retry(self, tmp_path):
        # stub pip：首次（无 flag）拒绝 externally-managed；重试必须带 flag 且成功。
        stub_dir, tree = self._make_tree(tmp_path)
        self._write_stub_pip(stub_dir, '''#!/usr/bin/env bash
if [ "$3" = "install" ]; then
  for a in "$@"; do
    if [ "$a" = "--break-system-packages" ]; then echo "installed with flag"; exit 0; fi
  done
  echo "error: externally-managed-environment" >&2
  exit 1
fi
if [ "$1" = "-c" ]; then eval "$2"; exit 0; fi
exit 0
''')
        env = {"PATH": f"{stub_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)}
        r = subprocess.run(["bash", str(tree / "scripts" / "install.sh")],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        assert "--break-system-packages" in r.stderr, "降级重试必须在 stderr 显式提示"
        assert "installed with flag" in r.stdout

    def test_flagged_retry_failure_fails_loudly(self, tmp_path):
        # 两次都失败（stub 恒拒绝）→ 脚本非零退出，不静默吞。
        stub_dir, tree = self._make_tree(tmp_path)
        self._write_stub_pip(stub_dir, '''#!/usr/bin/env bash
if [ "$3" = "install" ]; then
  echo "error: externally-managed-environment" >&2
  exit 1
fi
if [ "$1" = "-c" ]; then eval "$2"; exit 0; fi
exit 0
''')
        env = {"PATH": f"{stub_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)}
        r = subprocess.run(["bash", str(tree / "scripts" / "install.sh")],
                           capture_output=True, text=True, env=env)
        assert r.returncode != 0
        assert "externally-managed" in r.stderr

    def test_plain_success_never_adds_flag(self, tmp_path):
        # 常规环境 pip 首次成功 → 不得出现 --break-system-packages（最小干预）。
        stub_dir, tree = self._make_tree(tmp_path)
        self._write_stub_pip(stub_dir, '''#!/usr/bin/env bash
if [ "$3" = "install" ]; then echo "plain ok"; exit 0; fi
if [ "$1" = "-c" ]; then eval "$2"; exit 0; fi
exit 0
''')
        env = {"PATH": f"{stub_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)}
        r = subprocess.run(["bash", str(tree / "scripts" / "install.sh")],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        assert "--break-system-packages" not in r.stderr
        assert "plain ok" in r.stdout

    def test_firstinstall_equivalent_command_documented(self):
        # D2：脚本头注释必须文档化首装等效命令（含 PEP 668 flag），消除自举盲区。
        text = self.SCRIPT.read_text()
        assert "pip install -e" in text
        assert "--break-system-packages" in text
        assert "externally-managed" in text


class TestInstallShPathInjection:
    """SEE-1319 LOW-1：TOOLKIT_ROOT 含单引号/空格时验证行不得语法断裂。"""

    REPO = Path(__file__).parent.parent.parent
    SCRIPT = REPO / "scripts" / "install.sh"

    def test_single_quote_in_path_verify_still_passes(self, tmp_path):
        # 工具树路径含单引号——验证行必须经 env 传参，不能拼进 python 源码。
        quoted = tmp_path / "it's-a-worktree"
        tree = quoted / ".dev" / "qa-toolkit"
        (tree / "src" / "godot_qa_toolkit").mkdir(parents=True)
        (tree / "scripts").mkdir()
        (tree / "pyproject.toml").write_text("[project]\n")
        (tree / "src" / "godot_qa_toolkit" / "__init__.py").write_text("")
        shutil.copy2(self.SCRIPT, tree / "scripts" / "install.sh")

        stub_dir = tmp_path / "stubbin2"
        stub_dir.mkdir()
        stub = stub_dir / "python3"
        # stub pip：安装成功；verify 段 python3 -c 用 stub 的 godot_qa_toolkit
        # 替身模块（放在 stubbin2 下模拟 import 解析到本工作树 src）。
        stub.write_text('''#!/usr/bin/env bash
if [ "$3" = "install" ]; then exit 0; fi
if [ "$1" = "-c" ]; then
  # 用绝对路径调真实 python3——递归调 python3 会命中本 stub 造成 fork 炸弹。
  PYTHONPATH="$GQT_STUB_SRC" /usr/bin/python3 -c "$2"
  exit $?
fi
exit 0
''')
        stub.chmod(0o755)
        fake_pkg = tree / "src" / "godot_qa_toolkit"
        # stub 的真实 python3 不在 PATH 首位时用系统 python3 执行 -c 段，
        # 通过 PYTHONPATH 指向本工作树 src 让 import 解析成立。
        env = {"PATH": f"{stub_dir}:{os.environ['PATH']}",
               "HOME": str(tmp_path),
               "GQT_STUB_SRC": str(tree / "src"),
               "PYTHONPATH": str(tree / "src")}
        r = subprocess.run(["bash", str(tree / "scripts" / "install.sh")],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, f"verify failed on single-quote path: {r.stderr}"
        assert "done" in r.stdout

    def test_verify_line_uses_env_var_not_string_interp(self):
        # 契约：脚本验证段不得再把 $TOOLKIT_ROOT 直接插进 python -c 单引号表达式。
        text = self.SCRIPT.read_text()
        assert "GQT_TOOLKIT_ROOT" in text
        # 拼插反模式必须消除（单引号 python 表达式内的 $ 展开）
        assert "'$TOOLKIT_ROOT/src'" not in text
