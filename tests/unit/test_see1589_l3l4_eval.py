"""SEE-1589 §SPEC-010（A-L3/L4）评估依据：doctor 检出识别与多检出误报。

判据（L3）：
- 直接在 gqt 检出（pyproject + src/godot_qa_toolkit）内运行不再 skipped；
- 多检出但同 HEAD → `ok_multi_checkout`（exit 0），不再误报 mismatch；
- HEAD 不同 → 仍 mismatch（真错配不放过）。

L4（--dry-run 成本）为实测评估项，见当轮汇报附件，不在此断言时钟。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit import doctor


def _fake_checkout(root: Path) -> Path:
    (root / "src" / "godot_qa_toolkit").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='godot-qa-toolkit'\n")
    return root


def test_locate_toolkit_root_recognizes_gqt_checkout(tmp_path):
    checkout = _fake_checkout(tmp_path / "gqt")
    nested = checkout / "tests" / "unit"
    nested.mkdir(parents=True)
    assert doctor.locate_toolkit_root(nested) == checkout


def test_locate_toolkit_root_env_wins(tmp_path, monkeypatch):
    checkout = _fake_checkout(tmp_path / "gqt")
    monkeypatch.setenv("GQT_TOOLKIT_ROOT", str(checkout))
    assert doctor.locate_toolkit_root(tmp_path) == checkout


def test_multi_checkout_same_head_is_ok(tmp_path, monkeypatch):
    installed = _fake_checkout(tmp_path / "installed")
    current = _fake_checkout(tmp_path / "worktree")
    monkeypatch.setattr(doctor, "resolve_editable_location",
                        lambda: str(installed))
    monkeypatch.setattr(doctor, "locate_toolkit_root", lambda cwd: current)
    monkeypatch.setattr(doctor, "_gitlink_head", lambda root: "abc123")
    ok, detail = doctor.check_install(tmp_path)
    assert ok is True
    assert detail["status"] == "ok_multi_checkout"
    assert detail["gitlink_head"] == "abc123"


def test_multi_checkout_different_head_is_mismatch(tmp_path, monkeypatch):
    installed = _fake_checkout(tmp_path / "installed")
    current = _fake_checkout(tmp_path / "worktree")
    monkeypatch.setattr(doctor, "resolve_editable_location",
                        lambda: str(installed))
    monkeypatch.setattr(doctor, "locate_toolkit_root", lambda cwd: current)
    heads = {str(installed): "aaa", str(current): "bbb"}
    monkeypatch.setattr(doctor, "_gitlink_head", lambda root: heads[str(root)])
    ok, detail = doctor.check_install(tmp_path)
    assert ok is False
    assert detail["status"] == "mismatch"
