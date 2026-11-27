"""C3-R14：Revy QA 新登记四项缺陷的常驻回归。

- D-NEW-1（CRITICAL）：refresh 全程持 `<map>.lock`（锁内复检 fresh 即
  合并返回）；`.renew-tmp` 带进程唯一后缀；插桩写/恢复经项目级
  probe-rw.lock 互斥。
- D-NEW-2（HIGH）：缺 `test_verdicts` 的基线不许靠增量续新补出空表
  冒充守恒（§SPEC-025）。
- D-NEW-3（MEDIUM）：`measure map-update` 文件形态 roots / MeasurementError
  一族 → 具名 JSON、零 traceback。
- D-NEW-4（MEDIUM）：feature 内容摘出 wholesale hash（定案 4/§SPEC-016），
  feature 编辑走 feature_changed / feature_rerun 通道。
"""

import json

from pathlib import Path

import pytest


# ─────────────────────────────── D-NEW-1 ───────────────────────────────
def test_refresh_lock_is_reentrant(tmp_path):
    """refresh 全程持锁后，续新腿同进程再取同一把锁不得自阻塞。"""
    import sys
    from contextlib import redirect_stderr

    from godot_qa_toolkit.mutation.truth_select import _refresh_lock

    map_file = tmp_path / "attribution-map.json"
    map_file.write_text("{}")
    entered: list = []
    err = io_stderr()
    with redirect_stderr(err):
        with _refresh_lock(str(map_file)):
            with _refresh_lock(str(map_file)):
                entered.append(1)
    assert entered == [1]


def io_stderr():
    import sys

    return sys.stderr


def test_probe_rw_lock_serializes(tmp_path):
    """两个线程在 probe-rw.lock 上串行：临界段不交错（D-NEW-1 的
    源文件损坏即交错注入/恢复所致）。"""
    import threading
    import time

    from godot_qa_toolkit.measurement.backup import probe_rw_lock

    proj = tmp_path / "proj"
    proj.mkdir()
    order: list = []

    def worker(tag: str, hold: float) -> None:
        with probe_rw_lock(proj):
            order.append(f"{tag}-enter")
            time.sleep(hold)
            order.append(f"{tag}-exit")

    t1 = threading.Thread(target=worker, args=("a", 0.25))
    t2 = threading.Thread(target=worker, args=("b", 0.0))
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join()
    t2.join()
    assert order == ["a-enter", "a-exit", "b-enter", "b-exit"], order
    # flock 语义：锁文件持久存在（持有标记），互斥由顺序断言证明。
    assert (proj / ".qa-cache" / "probe-rw.lock").is_file()


def test_renew_tmp_name_is_process_unique(tmp_path, monkeypatch):
    """.renew-tmp 带进程唯一后缀：并发败者不再 FileNotFoundError、
    旧图字节不被误动（源码级钉子：唯一名拼装在场）。"""
    src = (Path(__file__).resolve().parents[2] / "src"
           / "godot_qa_toolkit" / "mutation" / "verdict_source.py")
    text = src.read_text(encoding="utf-8")
    assert ".renew-tmp.{" in text
    assert "os.getpid()" in text
    assert "uuid4().hex[:8]" in text


# ─────────────────────────────── D-NEW-2 ───────────────────────────────
def _baseline_doc(tmp_path: Path, with_table: bool) -> Path:
    from godot_qa_toolkit.measurement.increment import (
        ATTRIBUTION_SCHEMA,
    )

    doc = {
        "schema_version": ATTRIBUTION_SCHEMA,
        "report_recipe_hash": "r0",
        "recipe_hash_pristine": None,
        "project_root": str(tmp_path),
        "identity": "i",
        "causal": {"enabled": False},
        "file_sha256": {},
        "target_files": [],
        "tests_face": {},
        "arms": {"gut_full": {"test_to_lines": {},
                              "test_to_files": {},
                              "script_to_files": {}}},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 0, "probes_fired": 0,
                                    "vacuous": False}},
    }
    if with_table:
        doc["test_verdicts"] = {}
    out = tmp_path / "baseline-map.json"
    out.write_text(json.dumps(doc))
    return out


def test_increment_refuses_baseline_missing_test_verdicts(tmp_path):
    """缺 `test_verdicts` 的基线：增量续新入口具名拒绝（不许补出空表
    冒充守恒）。"""
    from godot_qa_toolkit.measurement.increment import (
        IncrementError, run_attribution_increment,
    )

    baseline = _baseline_doc(tmp_path, with_table=False)
    out = tmp_path / "out-map.json"
    with pytest.raises(IncrementError) as ei:
        run_attribution_increment(
            str(tmp_path), [], {}, str(baseline), [], str(out))
    assert "test_verdicts" in str(ei.value)
    assert "§SPEC-025" in str(ei.value)


def test_prepare_increment_renewal_refuses_missing_table(tmp_path):
    """verdict_source 增量续新入口同样具名拒绝（refresh 阶梯侧）。"""
    from godot_qa_toolkit.mutation.verdict_source import (
        BaselineSourceError, _prepare_increment_renewal,
    )

    baseline = _baseline_doc(tmp_path, with_table=False)
    doc = json.loads(baseline.read_text())
    with pytest.raises(BaselineSourceError) as ei:
        _prepare_increment_renewal(doc, str(tmp_path), str(baseline),
                                   [], 600)
    assert "test_verdicts" in str(ei.value)


# ─────────────────────────────── D-NEW-3 ───────────────────────────────
def _run_cli(argv: list, monkeypatch) -> tuple:
    import io
    import sys
    from contextlib import redirect_stdout, redirect_stderr

    from godot_qa_toolkit import cli as cli_mod

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_mod.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_map_update_file_shaped_roots_named_json(tmp_path, monkeypatch):
    """roots 传文件路径 → 空 rglob → 入口具名 JSON（此前裸 traceback、
    stdout 0 字节）。"""
    recipe = tmp_path / "recipe.json"
    recipe.write_text("{}")
    target = tmp_path / "f.gd"
    target.write_text("extends RefCounted\n")
    baseline = _baseline_doc(tmp_path, with_table=True)
    rc, out, err = _run_cli(
        ["measure", "map-update", "--project-root", str(tmp_path),
         "--recipe", str(recipe),
         "--baseline", str(baseline), "--out", str(tmp_path / "o.json"),
         "--changed", str(target), str(target)],
        monkeypatch)
    assert rc == 1
    assert "Traceback" not in err
    payload = json.loads(out)
    assert payload.get("ok") is False
    assert "no target files" in (payload.get("summary") or {}).get("error", "")


def test_map_update_measurement_error_named_json(tmp_path, monkeypatch):
    """`{}` recipe（recipe_version mismatch 一族 MeasurementError）→
    具名 JSON、零 traceback。"""
    recipe = tmp_path / "recipe.json"
    recipe.write_text("{}")
    baseline = _baseline_doc(tmp_path, with_table=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "g.gd").write_text("extends RefCounted\n")
    rc, out, err = _run_cli(
        ["measure", "map-update", "--project-root", str(tmp_path),
         "--recipe", str(recipe),
         "--baseline", str(baseline), "--out", str(tmp_path / "o.json"),
         "--changed", str(tmp_path), str(tmp_path / "src")],
        monkeypatch)
    assert rc == 1
    assert "Traceback" not in err
    payload = json.loads(out)
    assert payload.get("ok") is False
    assert (payload.get("summary") or {}).get("error")


# ─────────────────────────────── D-NEW-4 ───────────────────────────────
def _mk_recipe(feature_sha: str) -> dict:
    return {
        "schema_version": "measurement-recipe/2",
        "recipe_hash": None,
        "project_root": "/tmp/whatever",
        "gut_face": {"dirs": ["res://tests/a"], "config_sha256": "c0"},
        "gherkin_face": {
            "features": [{"key": "hotkey_h_toggle",
                          "source": "res://addons/gqt_gherkin/features/"
                                    "hotkey_h_toggle.feature"}],
            "feature_sha256": {"hotkey_h_toggle": feature_sha},
            "count": 1,
            "harness": {"worker": "generated/gherkin_worker.gd",
                        "ops_version": "1", "wiring_sha256": "w0"},
            "adapter_source_sha256": "ad0",
            "adapter_source_files": 1,
        },
        "toolchain": {"godot_version": "4.6.2"},
    }


def test_recipe_hash_ignores_feature_content():
    """feature 内容指纹摘出 wholesale hash：仅 feature_sha256 变化 →
    recipe_hash 不变（feature 编辑不再被判成环境失配）。"""
    from godot_qa_toolkit.measurement.recipe import recipe_hash

    a = _mk_recipe("sha-AAA")
    b = _mk_recipe("sha-BBB")
    assert recipe_hash(a) == recipe_hash(b)


def test_recipe_hash_changes_with_feature_structure():
    """结构面（features key/source）仍在 hash 内：feature 增删仍失配。"""
    from godot_qa_toolkit.measurement.recipe import recipe_hash

    a = _mk_recipe("sha-AAA")
    b = _mk_recipe("sha-AAA")
    b["gherkin_face"]["features"] = [
        {"key": "other_feature", "source": "res://x/other.feature"}]
    assert recipe_hash(a) != recipe_hash(b)


def test_feature_drift_marks_changed_feature(tmp_path):
    """feature 内容漂移进变更面（rel 用盘上路径，classify 前缀一致）；
    pristine 不失配。"""
    from godot_qa_toolkit.mutation.verdict_source import _prepare_drift_state
    import hashlib

    feats = tmp_path / "addons" / "gqt_gherkin" / "features"
    feats.mkdir(parents=True)
    f = feats / "hotkey_h_toggle.feature"
    f.write_text("Feature: old\n")
    doc = {
        "file_sha256": {}, "tests_face": {},
        # C3-R15：feature_sha256 在 map 产物顶层（_stamp_feature_sha 回填
        # / 增量产物携带），不在 recipe 体（map 不存 recipe）。
        "feature_sha256": {
            "hotkey_h_toggle": hashlib.sha256(
                "Feature: old\n".encode()).hexdigest()},
    }
    (tmp_path / "project.godot").write_text("config_version=5\n")
    changed, drift, pristine_ok = _prepare_drift_state(
        doc, str(tmp_path), [])
    # 未编辑：无 feature 漂移
    assert not [d for d in drift if d.startswith("feature_")]
    # 编辑后：changed 含盘上 rel、pristine 不失配（feature 不再进 hash）
    f.write_text("Feature: new\n")
    changed2, drift2, pristine_ok2 = _prepare_drift_state(
        doc, str(tmp_path), [])
    rel = "addons/gqt_gherkin/features/hotkey_h_toggle.feature"
    assert rel in changed2
    assert "feature_changed:hotkey_h_toggle" in drift2


def test_adapter_sha_excludes_features(tmp_path):
    """adapter 指纹只算 .gd：feature 文件变化不再改变 adapter_source_sha256
    （adapter .gd 代码变化仍全量）。"""
    from godot_qa_toolkit.measurement.recipe import adapter_source_sha256

    addon = tmp_path / "addons" / "gqt_gherkin"
    (addon / "features").mkdir(parents=True)
    (addon / "harness.gd").write_text("extends Node\n")
    (addon / "features" / "a.feature").write_text("Feature: v1\n")
    s1, n1 = adapter_source_sha256(tmp_path)
    (addon / "features" / "a.feature").write_text("Feature: v2 edited\n")
    s2, n2 = adapter_source_sha256(tmp_path)
    assert s1 == s2
    assert n1 == n2 == 1  # 只有 harness.gd
    (addon / "harness.gd").write_text("extends Node\n# changed\n")
    s3, _ = adapter_source_sha256(tmp_path)
    assert s3 != s1


def test_probe_lock_waits_indefinitely_until_released(tmp_path):
    """R15 退回项 1：持有者占用锁 3 秒（远超 R14 的 120s 假设也要等住）
    ——等待者在锁释放前不得进入临界段，释放后正常完成；全程无"超时后
    无锁继续"分支（stderr 零 proceeding 字样）。"""
    import subprocess
    import sys
    import time

    from godot_qa_toolkit.measurement.backup import probe_rw_lock

    proj = tmp_path / "proj"
    (proj / ".qa-cache").mkdir(parents=True)
    entered: list = []
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, time\n"
         "lf = open(%r, 'a+')\n"
         "fcntl.flock(lf.fileno(), fcntl.LOCK_EX)\n"
         "print('HELD', flush=True)\n"
         "time.sleep(3.0)\n"
         "fcntl.flock(lf.fileno(), fcntl.LOCK_UN)\n"
         % (str(proj / ".qa-cache" / "probe-rw.lock"),)],
        stdout=subprocess.PIPE, text=True)
    assert holder.stdout is not None
    holder.stdout.readline()  # 等 HELD
    t0 = time.monotonic()
    with probe_rw_lock(proj):
        entered.append(time.monotonic() - t0)
    holder.wait(timeout=30)
    assert len(entered) == 1
    assert entered[0] >= 2.0, f"entered too early: {entered[0]}s"


# ─────────────────────────────── C3-R15 ───────────────────────────────
def test_gdtoolkit_cache_warmup_survives_concurrency(tmp_path, monkeypatch):
    """R15 退回项 3：同一 HOME 下两个"进程"并发预热 gdtoolkit 缓存目录
    ——exist_ok 预热消除库内裸 makedirs 的 FileExistsError 竞争。"""
    import threading

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.delenv("GQT_GODOT_BIN", raising=False)
    import godot_qa_toolkit.measurement.gdtoolkit_compat as gc
    gc._WARMED = False
    errs: list = []

    def warm():
        try:
            gc.ensure_parser_cache_dir(force_home=str(home))
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    monkeypatch.setattr(gc, "_WARMED", False)
    threads = [threading.Thread(target=warm) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errs, errs


# ─────────────────────────────── C3-R16 ───────────────────────────────
def test_status_names_missing_feature_sha256(tmp_path):
    """R16 退回项 2：项目有 feature 而 map 无 `feature_sha256` 键（直建
    产物缺键 / 旧产物）→ status 具名因素、不得当 fresh。"""
    feats = tmp_path / "addons" / "gqt_gherkin" / "features"
    feats.mkdir(parents=True)
    (feats / "a.feature").write_text("Feature: A\n")
    doc = {"file_sha256": {}, "tests_face": {}}  # 无 feature_sha256 键
    from godot_qa_toolkit.mutation.map_contract import _feature_face_drift

    drift = _feature_face_drift(str(tmp_path), doc)
    assert drift is not None and "missing" in drift, drift


def test_status_missing_feature_sha256_predicts_full(tmp_path):
    """C3-R17（退回项 1 一行口径）：缺键因素的 would_refresh 必须如实
    预测 refresh 将执行的**全量重建**（与 schema/arms/recipe 同列），
    不得落进增量判定（因素文案自己写着 "full rebuild required"）。"""
    feats = tmp_path / "addons" / "gqt_gherkin" / "features"
    feats.mkdir(parents=True)
    (feats / "a.feature").write_text("Feature: A\n")
    doc = {"schema_version": "test-attribution/7",
           "file_sha256": {}, "tests_face": {},
           "arms": {"gut_full": {"test_to_files": {}}}}  # 无 feature_sha256 键
    import json as _json

    map_dir = tmp_path / ".qa-cache"
    map_dir.mkdir()
    (map_dir / "attribution-map.json").write_text(_json.dumps(doc))
    from godot_qa_toolkit.mutation.map_contract import status

    st = status(str(tmp_path), [])
    assert st.get("fresh") is False
    assert st.get("would_refresh") == "full", st.get("would_refresh")
    assert any("feature_sha256: missing" in f
               for f in st.get("factors") or [])


def test_attribution_artifact_carries_feature_sha256():
    """R16 退回项 2：`measure run --test-attribution` 直建产物顶层带
    `feature_sha256`（三条建图路径之一；之前只在落盘后二次改写）。"""
    from godot_qa_toolkit.measurement.attribution import (
        _attribution_artifact,
    )

    artifact = _attribution_artifact(
        "rh", "prh", Path("/tmp"), {}, {}, [], {}, {}, [], [],
        feature_sha256={"demo": "sha0"})
    assert artifact["feature_sha256"] == {"demo": "sha0"}


def test_two_real_processes_concurrent_coverage(tmp_path, monkeypatch):
    """R16 退回项 1：两个**真进程** + 干净 gdtoolkit 缓存并发 coverage
    ——进程级预热（cli.main 分发前 + Parser._get_parser wrap）兜住任何
    路径的第一次 parse()。双线程测试里 `_WARMED` 让第二次调用直接返回，
    测不到进程级竞争。"""
    import os as _os
    import shutil as _sh
    import subprocess as _sp
    import sys as _sys

    home = tmp_path / "home"
    home.mkdir()
    # 干净 gdtoolkit 缓存（竞争前提）
    cache = home / ".cache" / "gdtoolkit"
    if cache.exists():
        _sh.rmtree(cache)
    godot = _os.environ.get("GQT_GODOT_BIN", "")
    if not godot or not Path(godot).is_file():
        import shutil as _sh2

        godot = _sh2.which("godot")
    if not godot:
        pytest.skip("real godot unavailable")
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "tests" / "a").mkdir(parents=True)
    (proj / "project.godot").write_text("config_version=5\n")
    (proj / "src" / "calc.gd").write_text(
        "extends RefCounted\nfunc f(a):\n\treturn a\n")
    (proj / "tests" / "a" / "test_a.gd").write_text(
        "extends GutTest\nfunc test_a():\n\tpass\n")
    (proj / ".gutconfig.json").write_text(
        '{"dirs": ["res://tests/a"], "include_subdirs": false,'
        ' "prefix": "test_"}')
    import sys as _sys2
    _sys2.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata

    install_recipe(proj)
    ensure_addon_metadata(proj)
    gut = _os.environ.get("GQT_FORMS_GUT_DIR", "")
    if gut and (Path(gut) / "gut_cmdln.gd").is_file():
        _sh.copytree(gut, proj / "addons" / "gut")
    src_root = Path(__file__).resolve().parents[2] / "src"
    env = dict(_os.environ)
    env.update({"HOME": str(home),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "XDG_DATA_HOME": str(home / ".data"),
                "TMPDIR": str(tmp_path),
                "PYTHONPATH": str(src_root),
                "GQT_GODOT_BIN": godot})
    _sp.run([godot, "--headless", "--import", "--path", str(proj)],
            capture_output=True, timeout=300, env=env)
    # 竞争目标 = gdtoolkit 语法缓存目录（任意触发 parse 的命令同源）；
    # measure complexity 触发 gdtoolkit parse 且无 map 依赖——最小夹具
    # （无 gherkin 臂）上 coverage 必然 no_attribution_map，故用
    # complexity 承载真进程并发竞争；coverage ×10 轮形态在 KOL 判据跑。
    if cache.exists():
        _sh.rmtree(cache)  # 竞争前提：干净 gdtoolkit 缓存
    procs = [
        _sp.Popen(
            [_sys.executable, "-m", "godot_qa_toolkit.cli", "measure",
             "complexity", str(proj / "src")],
            stdout=_sp.PIPE, stderr=_sp.PIPE, text=True, env=env)
        for _ in range(2)]
    errs: list = []
    outs: list = []
    for i, pr in enumerate(procs):
        o, e = pr.communicate(timeout=300)
        errs.append(e or "")
        outs.append(o or "")
        assert pr.returncode == 0, (
            f"process {i} rc={pr.returncode}: stderr={errs[i][-400:]} "
            f"stdout={outs[i][-400:]}")
    joined = "\n".join(errs)
    assert "FileExistsError" not in joined, joined[-800:]
    assert "proceeding WITHOUT" not in joined
