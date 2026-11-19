"""SEE-1564 §SPEC-208 D-2 修正钉子：worker 路由目标恢复 coverage prefilter。

缺陷（Revy 复验实测）：coverage 写侧恒 cold 身份（coverage/runner 唯一调用
点，identity_pack 缺省），读侧全匹配校验；e89c23b 父层身份按通道解析后，
worker 路由目标（KOL systems 实测 13.5%）对 cold 条目恒 drift → prefilter
永久失效（e89c23b 引入的功能面回归）。

裁定修复（选项二）：coverage 是通道无关事实——prefilter 查找/校验改用
coverage 通道身份（cold/canonical）；manifest/store2 键维持通道身份不动；
§3.6 证书 scheme 隔离不动。红线：其余身份成分（ops/godot 版本/wiring/
gut 哈希）仍全匹配校验——身份不符仍禁用并披露（宁 miss 不假绿）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation.coverlink import save_coverage
from godot_qa_toolkit.mutation.executor import run_mutation_files
from godot_qa_toolkit.mutation.identity import identity_pack
from godot_qa_toolkit.mutation.inject import detect_binary_kind
from godot_qa_toolkit.mutation.worker_channel import WORKER_SCHEME
import godot_qa_toolkit.mutation.runner as runner_mod
import godot_qa_toolkit.mutation.worker_channel as worker_channel_mod

SRC = ("func add(a, b):\n"
       "\treturn a + b\n"
       "\n"
       "func sub(a, b):\n"
       "\treturn a - b\n")

KILL_TAIL = "* test_killed_by_probe\n\t\t[Failed]\n"


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    # 杀手可归因（唯一命中）——证书真实签发，避免证书门噪音干扰本钉子。
    (proj / "tests").mkdir()
    (proj / "tests" / "test_forms.gd").write_text(
        "func test_killed_by_probe():\n\tpass\n")
    target = proj / "calc.gd"
    target.write_text(SRC)
    # SEE-1594 A05：store 层种子 = /2 绑定当前映射——recipe 必须可构建
    import attribution_fixtures as _fx
    from godot_qa_toolkit.mutation import truth_select as _truth
    _fx.install_recipe(proj)
    _truth._fresh_recipe_hash.cache_clear()
    return proj, target


def _cold_ident(proj):
    """与 coverage 写侧同源：identity_pack 缺省即 cold（canonical）。"""
    return identity_pack(str(proj), detect_binary_kind())


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_worker_fakes(monkeypatch):
    """强制 worker-reset 路由 + 腿执行以冷腿承接（键/身份机制全真实）。"""
    monkeypatch.setattr(worker_channel_mod, "resolve_exec_scheme",
                        lambda *a, **k: (WORKER_SCHEME, None))
    monkeypatch.setattr(runner_mod, "resolve_exec_scheme",
                        lambda *a, **k: (WORKER_SCHEME, None))

    def shim(path, project_root, original_src, mutants, tests_glob,
             baseline_failures, baseline_dirty, slot, auto_name,
             target_rel, target_res, kind, reporter, store2,
             prior_entries, scopes, ownership_fn, timings_out=None,
             kill_bail=False, tests_scripts=None):
        raw = runner_mod._run_cold_legs(
            path, project_root, original_src, "", mutants, tests_glob,
            baseline_failures, baseline_dirty, slot, auto_name, target_rel,
            target_res, ownership_fn, reporter, store2, prior_entries, scopes)
        return raw, []

    monkeypatch.setattr(worker_channel_mod, "execute_worker_branch", shim)
    monkeypatch.setattr(runner_mod, "_run_gut_on_project",
                        lambda root, timeout_s=60, tests_glob=None:
                            (1, KILL_TAIL))
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})


def test_worker_route_prefilter_hits_cold_coverage(tmp_path, monkeypatch,
                                                   capsys):
    """worker 目标 + cold /2 投影条目（绑定映射，glob 对齐）→ prefilter
    生效：line2（covered）执行、line5（instrumentable ∧ uncovered）显式
    跳过。"""
    _install_worker_fakes(monkeypatch)
    proj, target = _write_project(tmp_path)
    import attribution_fixtures as _fx
    _fx.seed_projection(
        proj, target, {"test_forms.gd::t1": {"calc.gd": [2]}},
        scripts=["res://tests/test_forms.gd"],
        instrumentable={"calc.gd": [2, 5]}, identity=_cold_ident(proj))
    capsys.readouterr()  # 清掉 seed 期输出

    # S2.1：行为钉走 selection 真实解析路径（显式 --tests 不再消费 map 派生层）
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    result = run_mutation_files([str(target)], str(proj), budget=10,
                                timeout_s=60, jobs=2,
                                select_mode="line", reuse_coverage=True)
    r = result["results"][0]
    s = r["summary"]
    err = capsys.readouterr().err
    assert "identity drift" not in err, (
        "worker-route prefilter must accept canonical cold coverage "
        f"(D-2 drift regression): {err[-400:]}")
    # SRC 位点：line2 ×3（covered → 执行）、line5 ×3（instrumentable ∧
    # uncovered → 显式跳过）。
    pf = s.get("coverage_prefilter")
    assert isinstance(pf, dict)
    assert pf["uncovered"] == 3 and pf["covered_executed"] == 3
    assert pf["source"] in ("projection_live", "projection_store")
    uncovered = [f for f in r["failures"] if f.get("verdict") == "uncovered"]
    assert len(uncovered) == 3 and all(f["line"] == 5 for f in uncovered)
    assert s["mutants"] == 3 and s["killed"] == 3


def test_worker_route_prefilter_still_rejects_real_identity_drift(
        tmp_path, monkeypatch, capsys):
    """红线：除 exec_scheme 外的身份成分仍全匹配校验——真 drift 仍禁用并
    披露（宁 miss 不假绿，本修复不得把校验弱化为跳过）。"""
    _install_worker_fakes(monkeypatch)
    proj, target = _write_project(tmp_path)
    stale = {**_cold_ident(proj), "godot_version": "bogus-drift"}
    import attribution_fixtures as _fx
    _fx.seed_projection(
        proj, target, {"test_forms.gd::t1": {"calc.gd": [2]}},
        scripts=["res://tests/test_forms.gd"],
        instrumentable={"calc.gd": [2, 5]}, identity=stale)
    capsys.readouterr()

    # S2.1：显式 --tests 下盘上漂移条目不参与消费——不过滤全量跑，
    # 漂移原因聚合披露可见（读拒绝单点钉在解析链，见 line_mutation_cli 套件）
    result = run_mutation_files([str(target)], str(proj), budget=10,
                                timeout_s=60, tests_glob="res://tests/",
                                jobs=2, reuse_coverage=True)
    s = result["results"][0]["summary"]
    err = capsys.readouterr().err
    assert "identity drift" in err  # 真漂移必须仍被抓住并披露
    assert "selection resolved no attribution map" in err
    pf = s.get("coverage_prefilter")
    assert pf is None or isinstance(pf, str)  # miss 态，不过滤
    assert s["mutants"] == 6  # 全部 6 位点回退执行
