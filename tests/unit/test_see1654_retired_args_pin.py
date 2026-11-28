"""SEE-1654 harden-R1 钉点①（冒烟矩阵缺常驻的行）：R16 / R17 行为钉。

冒烟矩阵（§SPEC-016 / 029 / 036 / 037，20 行）此前只有手工附件脚本；
其中 R01 / R05 / R11 / R13 / R14 / R15 已有常驻等价物
（`test_c3r19_map_contract_fixes_see1645.py` 等）。本模块补齐此前
零常驻覆盖的两行：

- R16：决策 1 退役的四个入口（`--attribution-map` /
  `--no-attribution-auto` / `measure run --test-attribution-out` /
  `measure map-update --baseline`）都是 argparse 用法错（exit 2 +
  unrecognized arguments）；环境变量 `GQT_ATTRIBUTION_MAP` 被无视，
  命令照常消费项目唯一地图（以 `measure map-update` 证明：mode=fresh
  且 map_path = 项目地图——若 env 被当地图来源， nonexistent 路径会
  走显式地图读失败出口，两种形态可区分）。
- R17：`measure map-update` 不再接收 roots / `--baseline` / `--out` /
  `--recipe` / `--changed`（exit 2）。

红绿咬合：四个退役参数钉在 `775ebdd`（基点 master，退役前——参数仍
在 CLI 面、argparse 接受）上为红（实得 rc != 2）；env 无视钉在
`775ebdd` 上为红（ENV_MAP 通道尚在，env 被当地图来源走显式失败出口，
mode != fresh）。在 `d961ccd` 及之后全绿。
"""

from __future__ import annotations

import json

import pytest


def _expect_argparse_exit_2(capsys, argv):
    """CLI 解析退役参数必须 argparse exit 2 + unrecognized arguments
    （不进任何处理函数）。"""
    from godot_qa_toolkit import cli as cli_mod

    with pytest.raises(SystemExit) as ei:
        cli_mod.main(argv)
    assert ei.value.code == 2, ei.value.code
    captured = capsys.readouterr()
    assert "unrecognized arguments" in (captured.err + captured.out), \
        (captured.err + captured.out)[:400]


def test_r16_retired_flags_are_argparse_errors(tmp_path, capsys):
    """R16：决策 1 退役的四个入口全部 argparse 用法错（exit 2）——
    mutation 的 `--attribution-map` / `--no-attribution-auto`、
    `measure run --test-attribution-out`、`measure map-update --baseline`。
    （argparse 在任何 I/O 之前拒绝——无需项目夹具。）"""
    root = str(tmp_path / "proj")
    for argv in (
        ["mutation", "prod.gd", "--project-root", root,
         "--attribution-map", "x.json"],
        ["mutation", "prod.gd", "--project-root", root,
         "--no-attribution-auto"],
        ["measure", "run", str(tmp_path / "prod.gd"), "--recipe",
         "/nonexistent.json", "--out", str(tmp_path / "r.json"),
         "--test-attribution-out", str(tmp_path / "a.json")],
        ["measure", "map-update", "--project-root", root,
         "--baseline", "x.json"],
    ):
        _expect_argparse_exit_2(capsys, argv)


def test_r16_env_map_variable_ignored_uses_project_map(tmp_path,
                                                       monkeypatch):
    """R16③：`GQT_ATTRIBUTION_MAP` 环境变量被无视——命令照常消费项目
    唯一地图（mode=fresh / already_fresh / map_path=项目地图）。
    （冒烟 R16 用 measure coverage 证明同一判据；此处用 map-update，
    零 godot 依赖即可区分两种形态。）"""
    from test_c3r19_map_contract_fixes_see1645 import (
        _bypass_entry_helpers,
        _fresh_doc,
        _make_project,
        _run_cli,
        _write_map,
    )

    proj = _make_project(tmp_path)
    map_file = _write_map(proj, _fresh_doc(proj))
    _bypass_entry_helpers(monkeypatch, proj)
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP", "/nonexistent/map.json")
    rc, out, err = _run_cli(["measure", "map-update",
                             "--project-root", str(proj)])
    assert rc == 0, err
    assert "Traceback" not in err
    payload = json.loads(out)
    assert payload.get("ok") is True, payload
    summary = payload.get("summary") or {}
    assert summary.get("mode") == "fresh", summary
    assert summary.get("already_fresh") is True, summary
    assert summary.get("map_path") == str(map_file), summary


def test_r17_map_update_rejects_roots_and_legacy_args(tmp_path, capsys):
    """R17：map-update 不收位置 roots / `--baseline` / `--out` /
    `--recipe` / `--changed`（全部 exit 2；决策 1：薄适配无手工增量）。"""
    root = str(tmp_path / "proj")
    for argv in (
        ["measure", "map-update", str(tmp_path / "prod.gd"),
         "--project-root", root],
        ["measure", "map-update", "--project-root", root,
         "--baseline", "x.json", "--out", "y.json", "--recipe", "r.json",
         "--changed", "c", str(tmp_path / "prod.gd")],
    ):
        _expect_argparse_exit_2(capsys, argv)
