"""SEE-1550 §SPEC-020 ③——W 通道六形态等价电池（真实引擎，资格期判据）。

六个 worker-eligible 形态（runtime_load / preload_late / class_name /
inner_class / scene_script / composition_root）：worker 通道与冷 oracle
逐位对比——verdict / kill 集合 / stable-id 必须全等（§SPEC-020 DoD）。
autoload / preload_boot 按定案 J3 回退归冷（路由单测已钉，此处不重跑）。

环境守卫（CI 无引擎 → 模块级 skip，非红）与 forms BDD 同型。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.runner import run_mutation

REPO = Path(__file__).resolve().parents[2]
FIXTURE_TEMPLATE = REPO / "tests" / "fixtures" / "see1550_forms"

_GUT_SOURCES = [
    os.environ.get("GQT_FORMS_GUT_DIR", ""),
    "/mnt/d/GodotProjects/king-of-likes/addons/gut",
    str(Path.home() / "tools" / "KingOfLikes-Godot" / "addons" / "gut"),
]

ELIGIBLE_FORMS = (
    "res://forms/runtime_load.gd",
    "res://forms/preload_late.gd",
    "res://forms/class_name_form.gd",
    "res://forms/inner_class_form.gd",
    "res://forms/scene_script.gd",
    "res://forms/composition_root.gd",
)


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
    _skip = "real Godot engine unavailable (CI) — worker battery skipped"
elif _gut_addon_source() is None:
    _skip = ("GUT addon source not found (set GQT_FORMS_GUT_DIR) — "
             "worker battery skipped")
if _skip:
    pytest.skip(_skip, allow_module_level=True)


@pytest.fixture(scope="module")
def battery_project(tmp_path_factory):
    proj = tmp_path_factory.mktemp("see1550_worker") / "proj"
    shutil.copytree(FIXTURE_TEMPLATE, proj)
    shutil.copytree(_gut_addon_source(), proj / "addons" / "gut")
    godot = os.environ.get("GQT_GODOT_BIN") or shutil.which("godot")
    subprocess.run([godot, "--headless", "--import", "--path", str(proj)],
                   capture_output=True, text=True, timeout=300,
                   cwd=str(proj), check=False)
    return proj


def _verdict_map(payload: dict) -> dict:
    """(id → verdict) 全息面：failures 记录 + killed 证书 id。"""
    results = payload.get("results") or [payload]
    out = {}
    for r in results:
        for f in r.get("failures", []):
            if "verdict" in f:
                out[f.get("id", f.get("file"))] = f["verdict"]
        for cert in r.get("summary", {}).get("killed_by_certificates", []):
            out[cert["id"]] = "killed"
    return out


@pytest.mark.parametrize("target_res", ELIGIBLE_FORMS)
def test_worker_matches_cold_oracle(battery_project, target_res):
    """worker 通道 vs 冷 oracle：verdict 分区/kill 集合/stable-id 逐位一致。"""
    target_fs = str(battery_project / target_res.removeprefix("res://"))
    common = dict(budget=30, timeout_s=180, mutate_all=True)
    cold = run_mutation(target_fs, str(battery_project),
                        worker_channel=False, **common)
    worker = run_mutation(target_fs, str(battery_project),
                          worker_channel=True, **common)
    assert cold["ok"] is True, cold["summary"]
    assert worker["ok"] is True, worker["summary"]
    # scheme 标签
    assert cold["summary"].get("exec_scheme") == "cold-shadow/1"
    assert worker["summary"].get("exec_scheme") == "worker-reset/1"
    # kill 面逐位一致（stable-id 级）
    c_map, w_map = _verdict_map(cold), _verdict_map(worker)
    assert c_map == w_map, (target_res, c_map, w_map)
    assert cold["summary"]["mutants"] == worker["summary"]["mutants"]
    assert cold["summary"]["killed"] == worker["summary"]["killed"]
