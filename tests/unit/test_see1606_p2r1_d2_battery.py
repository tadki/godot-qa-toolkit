"""SEE-1606 QA 回修集成判据（真实 GUT 宿主，非 fake）。

D1：resident worker 默认路由 + 顺序执行 + kill-bail 开启——必须在限定
时间内产出完整 JSON verdict（此前挂死 >90s）。
D2：基线红测物理跳过按「脚本归属 + 测试名」定位——跨脚本同名反例中仅
红脚本的同名测试被摘除，绿脚本同名 killer 照跑并维持 killed 集合
（cold direct-drive 与 worker 可控宿主两路均覆盖）。

环境守卫与 see1550 worker battery 同型（无真实 Godot/GUT → skip）。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FORMS_TEMPLATE = REPO / "tests" / "fixtures" / "see1550_forms"

_GUT_SOURCES = [
    os.environ.get("GQT_FORMS_GUT_DIR", ""),
    "/mnt/d/GodotProjects/king-of-likes/addons/gut",
    str(Path.home() / "tools" / "KingOfLikes-Godot" / "addons" / "gut"),
]


def _godot_available() -> bool:
    if os.environ.get("GQT_GODOT_BIN", "").strip():
        return Path(os.environ["GQT_GODOT_BIN"]).is_file()
    return shutil.which("godot") is not None


def _gut_addon_source() -> Path | None:
    for cand in _GUT_SOURCES:
        p = Path(cand) if cand else None
        if p is not None and (p / "gut_cmdln.gd").is_file():
            return p
    return None


_skip = None
if not _godot_available():
    _skip = "real Godot engine unavailable (CI) — D1/D2 battery skipped"
elif _gut_addon_source() is None:
    _skip = ("GUT addon source not found (set GQT_FORMS_GUT_DIR) — "
             "D1/D2 battery skipped")
if _skip:
    pytest.skip(_skip, allow_module_level=True)

from godot_qa_toolkit.mutation import runner as mrunner  # noqa: E402
from godot_qa_toolkit.mutation.runner import run_mutation  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_baseline_red_map():
    """§SPEC-P2-R1-D3：battery 在进程内写入模块态 _BASELINE_RED_MAP
    （真实脏基线），teardown 清场——直调分类器的隔离用例不得读取
    陈旧全局（测试套件环境无关确定性）。"""
    yield
    mrunner._BASELINE_RED_MAP = set()

TARGET_SRC = (
    "extends RefCounted\n\n\n"
    "func sign(x: int) -> bool:\n"
    "\treturn x > 0\n"
)

# 脚本 A：基线红 test_shared（恒败）；脚本 B：绿 test_shared = sign 的
# `>`→`>=` 变异 killer。同名跨脚本（D2 反例核心）。
TEST_B_GREEN_KILLER = (
    "extends GutTest\n\n\n"
    "func test_shared() -> void:\n"
    "\tvar t := load(\"res://calc_sign.gd\")\n"
    "\tvar inst: RefCounted = t.new()\n"
    "\tassert_eq(inst.sign(0), false)\n"
)


def _build_project(tmp_path_factory) -> Path:
    proj = tmp_path_factory.mktemp("see1606_d2") / "proj"
    shutil.copytree(FORMS_TEMPLATE, proj)
    shutil.copytree(_gut_addon_source(), proj / "addons" / "gut")
    (proj / "calc_sign.gd").write_text(TARGET_SRC, encoding="utf-8")
    tests = proj / "tests" / "d2"
    tests.mkdir(parents=True)
    (tests / "test_a_red.gd").write_text(
        "extends GutTest\n\n\n"
        "func test_shared() -> void:\n"
        "\tfail_test(\"baseline red by design\")\n", encoding="utf-8")
    (tests / "test_b_green.gd").write_text(TEST_B_GREEN_KILLER,
                                            encoding="utf-8")
    godot = os.environ.get("GQT_GODOT_BIN") or shutil.which("godot")
    subprocess.run([godot, "--headless", "--import", "--path", str(proj)],
                   capture_output=True, text=True, timeout=300,
                   cwd=str(proj), check=False)
    return proj


def _materialize(proj: Path, names: list[str]) -> str:
    sel_dir = proj / ".gqt-attr-select" / "d2sel"
    sel_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (sel_dir / n).write_text(
            f'extends "res://tests/d2/{n}"\n', encoding="utf-8")
    return "res://.gqt-attr-select/d2sel/"


@pytest.fixture(scope="module")
def d2_project(tmp_path_factory):
    return _build_project(tmp_path_factory)


def _verdict_map(payload: dict) -> dict:
    results = payload.get("results") or [payload]
    out = {}
    for r in results:
        for f in r.get("failures", []):
            if "verdict" in f:
                out[f.get("id", f.get("file"))] = f["verdict"]
        for cert in r.get("summary", {}).get("killed_by_certificates", []):
            out[cert["id"]] = "killed"
    return out


class TestD1WorkerSequentialBailRealHost:
    def test_worker_sequential_bail_produces_verdict(self, d2_project):
        """D1：worker + 顺序执行 + bail 开启 → 数秒内完整 JSON verdict。"""
        target_fs = str(d2_project / "forms" / "runtime_load.gd")
        r = run_mutation(target_fs, str(d2_project),
                         worker_channel=True, tests_glob="res://tests/",
                         budget=2, mutate_all=True, timeout_s=120)
        assert r["summary"].get("run_error") is None, r["summary"]
        assert r["summary"].get("exec_scheme") == "worker-reset/1"
        assert r["summary"].get("killed", 0) >= 1
        assert r["ok"] is True

    def test_worker_sequential_bail_parity_with_no_bail(self, d2_project):
        """bail on/off verdict 集一致（早停只省成本不改判定）。"""
        target_fs = str(d2_project / "forms" / "runtime_load.gd")
        common = dict(worker_channel=True, tests_glob="res://tests/",
                      budget=2, mutate_all=True, timeout_s=120)
        on = run_mutation(target_fs, str(d2_project), **common)
        off = run_mutation(target_fs, str(d2_project),
                           kill_bail=False, **common)
        assert on["ok"] is True and off["ok"] is True
        assert _verdict_map(on) == _verdict_map(off)


class TestD2SkipRedScriptQualified:
    def test_cold_skips_only_red_script_same_name(self, d2_project):
        """cold 直驱：同名跨脚本仅红脚本的同名测试被摘除。"""
        glob = _materialize(d2_project, ["test_a_red.gd", "test_b_green.gd"])
        target_fs = str(d2_project / "calc_sign.gd")
        r = run_mutation(target_fs, str(d2_project), budget=10,
                         mutate_all=True, tests_glob=glob,
                         worker_channel=False, timeout_s=120)
        s = r["summary"]
        # D2 机证：requested 1（红脚本 A 的 test_shared）恰好 marked 1
        assert s.get("baseline_skip", {}).get("requested") == 1
        # killed 集合非空（脚本 B 的绿 killer 照跑并杀死 `>`→`>=` 变异）
        assert s.get("killed", 0) >= 1
        # 无假 survivor：sign_of 位点全部有判定
        assert s.get("mutants", 0) == (
            s.get("killed", 0) + s.get("survived", 0) + s.get("suspect", 0)
            + s.get("run_errors", 0) + s.get("invalid_mutants", 0))

    def test_worker_skips_only_red_script_same_name(self, d2_project):
        """worker 可控宿主：同上（D2 两路覆盖）。"""
        glob = _materialize(d2_project, ["test_a_red.gd", "test_b_green.gd"])
        target_fs = str(d2_project / "calc_sign.gd")
        r = run_mutation(target_fs, str(d2_project), budget=10,
                         mutate_all=True, tests_glob=glob,
                         worker_channel=True, timeout_s=120)
        s = r["summary"]
        assert s.get("baseline_skip", {}).get("requested") == 1
        assert s.get("killed", 0) >= 1
        assert s.get("mutants", 0) == (
            s.get("killed", 0) + s.get("survived", 0) + s.get("suspect", 0)
            + s.get("run_errors", 0) + s.get("invalid_mutants", 0))

    def test_cold_worker_verdict_parity_same_name(self, d2_project):
        """D2 parity：cold 与 worker 在同名反例上 verdict/kill 集一致。"""
        glob = _materialize(d2_project, ["test_a_red.gd", "test_b_green.gd"])
        target_fs = str(d2_project / "calc_sign.gd")
        common = dict(budget=10, mutate_all=True, tests_glob=glob,
                      timeout_s=120)
        cold = run_mutation(target_fs, str(d2_project),
                            worker_channel=False, **common)
        worker = run_mutation(target_fs, str(d2_project),
                              worker_channel=True, **common)
        assert _verdict_map(cold) == _verdict_map(worker)
        assert cold["summary"].get("killed") == worker["summary"].get("killed")
