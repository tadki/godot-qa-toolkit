"""SEE-1624 DEF-B — 多 key 单次 `compile --keys --out-dir` 回归。

判据：
- 多 key + 项目内空目录：同一次调用可写多个 key（不再被第一个 key 的
  产物触发 "out_dir must be empty"）；
- 调用方预先存在文件/目录：具名拒绝且 caller 数据零删除、零写入；
- 单 key `--out-dir`（空目录）不回归；
- 自动私有套件（compile_keys_auto_suite）不回归。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gherkin_fixtures import make_addon_project, write_feature


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = make_addon_project(tmp_path / "proj")
    write_feature(root, "alpha")
    write_feature(root, "beta")
    return root


def test_multi_key_writes_all_keys_in_one_call(project: Path) -> None:
    from godot_qa_toolkit.gherkin.suite import compile_keys_to_suite

    out = project / "suite-out"
    out.mkdir()
    record = compile_keys_to_suite(project, ["alpha", "beta"], out)
    assert record["keys"] == ["alpha", "beta"]
    gen = out / "generated"
    for key in ("alpha", "beta"):
        assert (gen / f"test_{key}.gd").is_file()
        assert (gen / f"{key}.manifest.json").is_file()


def test_multi_key_rejects_preexisting_caller_file(project: Path) -> None:
    from godot_qa_toolkit.gherkin.suite import SuiteError, compile_keys_to_suite

    out = project / "caller-out"
    out.mkdir()
    (out / "notes.txt").write_text("caller data", encoding="utf-8")
    with pytest.raises(SuiteError, match="out_dir must be empty"):
        compile_keys_to_suite(project, ["alpha", "beta"], out)
    # caller 数据零删除、零写入
    assert (out / "notes.txt").read_text(encoding="utf-8") == "caller data"
    assert not (out / "generated").exists()


def test_multi_key_rejects_preexisting_caller_subdir(project: Path) -> None:
    from godot_qa_toolkit.gherkin.suite import SuiteError, compile_keys_to_suite

    out = project / "caller-sub"
    (out / "keep").mkdir(parents=True)
    with pytest.raises(SuiteError, match="out_dir must be empty"):
        compile_keys_to_suite(project, ["alpha", "beta"], out)
    assert (out / "keep").is_dir()


def test_single_key_out_dir_still_works(project: Path) -> None:
    from godot_qa_toolkit.gherkin.compile import compile_feature

    out = project / "single"
    out.mkdir()
    suite, error = compile_feature(
        project / "addons" / "gqt_gherkin" / "features" / "alpha.feature",
        project, write=True, out_dir=out)
    assert error is None
    assert (out / "generated" / "test_alpha.gd").is_file()


def test_single_key_non_empty_out_dir_still_rejected(project: Path) -> None:
    from godot_qa_toolkit.gherkin.compile import compile_feature

    out = project / "nonempty"
    out.mkdir()
    (out / "notes.txt").write_text("keep", encoding="utf-8")
    suite, error = compile_feature(
        project / "addons" / "gqt_gherkin" / "features" / "alpha.feature",
        project, write=True, out_dir=out)
    assert suite is None and error is not None
    assert "out_dir must be empty" in error["reason"]
    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_auto_private_suite_not_regressed(project: Path) -> None:
    from godot_qa_toolkit.gherkin.suite import compile_keys_auto_suite

    record = compile_keys_auto_suite(project, ["alpha", "beta"])
    suite_dir = Path(record["suite_dir"])
    assert suite_dir.is_dir()
    assert suite_dir.name.startswith(".gqt-run-")
    assert (suite_dir / "generated" / "test_beta.gd").is_file()
