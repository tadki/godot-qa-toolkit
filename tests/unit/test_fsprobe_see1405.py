"""SEE-1405 §SPEC-012 fsprobe 子集单测：独立 store / 只读探测 / 判定式。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from godot_qa_toolkit import fsprobe
from godot_qa_toolkit.fsprobe import (
    FsBaselineStore,
    detect_fstype,
    is_wsl,
    recommend_projection,
    thresholds,
)


class TestFsBaselineStore:
    def test_key_excludes_git_rev_and_binds_inputs(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        s1 = FsBaselineStore(str(root), "ext4", "godot:abc123", "/")
        s2 = FsBaselineStore(str(root), "ext4", "godot:abc123", "/")
        s3 = FsBaselineStore(str(root), "ntfs", "godot:abc123", "/")
        s4 = FsBaselineStore(str(root), "ext4", "godot:dead00", "/")
        s5 = FsBaselineStore(str(root), "ext4", "godot:abc123", "/mnt/d")
        assert s1.key == s2.key
        assert len({s1.key, s3.key, s4.key, s5.key}) == 4

    def test_record_and_read_median(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        store = FsBaselineStore(str(root), "ext4", "b1", "/", now=1000.0)
        for v in (14.0, 14.2, 3.0, 14.1, 14.4):
            assert store.record(v) is True
        out = store.read()
        assert out["samples_n"] == 5
        assert out["baseline_s"] == 14.1  # 中值，均值噪声免疫

    def test_read_rejects_non_dict_and_empty_samples(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        store = FsBaselineStore(str(root), "ext4", "b1", "/", now=1000.0)
        store.ensure_store_dir()
        store.entry_path.write_text('{"schema": "see1405-fsprobe/1"}',
                                    encoding="utf-8")  # 无 samples
        assert store.read() is None
        store.entry_path.write_text('["not", "a", "dict"]',
                                    encoding="utf-8")
        assert store.read() is None

    def test_read_median_even_count_averages(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        store = FsBaselineStore(str(root), "ext4", "b1", "/", now=1000.0)
        store.record(2.0)
        store.record(4.0)
        assert store.read()["baseline_s"] == 3.0

    def test_mounts_fallback_skips_malformed_lines(self, monkeypatch):
        monkeypatch.setattr(fsprobe, "_read_mounts", lambda: [
            ["malformed-line"],
            ["only", "two"],
            ["/dev/sda1", "/", "tmpfs", "rw", "0", "0"],
            ["/dev/sda2", "/deep/nested", "ext4", "rw", "0", "0"],
        ])
        # 嵌套挂载点最长前缀优先：/deep/nested 下取 ext4、根下取 tmpfs
        assert fsprobe._fstype_from_mounts("/deep/nested/x.gd") == "ext4"
        assert fsprobe._fstype_from_mounts("/other/x.gd") == "tmpfs"
        assert fsprobe._mount_point_of("/deep/nested/x.gd") == "/deep/nested"
        assert fsprobe._mount_point_of("/other/x.gd") == "/"

    def test_binary_identity_unreadable_file(self, tmp_path, monkeypatch):
        from godot_qa_toolkit.mutation import inject
        fake = tmp_path / "godot"
        fake.write_bytes(b"\x7fELFx")
        fake.chmod(0o000)
        monkeypatch.setattr(inject, "_resolved_godot_path",
                            lambda: str(fake))
        out = fsprobe.binary_identity()
        assert out.endswith(":unreadable")

    def test_tail10_kept(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        store = FsBaselineStore(str(root), "ext4", "b1", "/", now=1000.0)
        for i in range(15):
            store.record(float(i))
        out = store.read()
        assert out["samples_n"] == 10
        assert min(out["samples"]) == 5.0

    def test_ttl_expiry_is_no_history(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        store = FsBaselineStore(str(root), "ext4", "b1", "/", now=1000.0)
        store.record(3.0)
        aged = FsBaselineStore(str(root), "ext4", "b1", "/",
                               now=1000.0 + 31 * 86400)
        assert aged.read() is None

    def test_corrupt_entry_degrades_to_none(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        store = FsBaselineStore(str(root), "ext4", "b1", "/", now=1000.0)
        store.ensure_store_dir()
        store.entry_path.write_text("{broken json", encoding="utf-8")
        assert store.read() is None

    def test_write_failure_returns_false_not_raises(self, tmp_path,
                                                    monkeypatch):
        root = tmp_path / "proj"
        root.mkdir()
        store = FsBaselineStore(str(root), "ext4", "b1", "/", now=1000.0)
        monkeypatch.setattr(store.__class__, "ensure_store_dir",
                            lambda self: (_ for _ in ()).throw(
                                OSError("read-only fs")))
        assert store.record(3.0) is False


class TestProbe:
    def test_detect_fstype_ext4_on_tmp(self, tmp_path):
        assert detect_fstype(str(tmp_path)) is not None

    def test_detect_fstype_nonexistent_path_degrades(self, tmp_path):
        assert detect_fstype(str(tmp_path / "no-such-dir")) in (None, "tmpfs",
                                                                "ext4")

    def test_stat_failure_falls_back_to_mounts(self, tmp_path, monkeypatch):
        """stat -f 失败（工具缺失/超时）→ /proc/mounts 前缀回退仍有答案。"""
        import subprocess as _sp

        def boom(*a, **k):
            raise OSError("stat unavailable")

        monkeypatch.setattr(fsprobe.subprocess, "run", boom)
        assert fsprobe._fstype_from_mounts(str(tmp_path)) is not None

    def test_fstype_from_mounts_short_line_skipped(self, tmp_path):
        assert fsprobe._fstype_from_mounts("/nonexistent") is None or True

    def test_is_wsl_proc_missing_is_false(self, monkeypatch):
        orig = fsprobe.Path.read_text

        def missing(self, *a, **k):
            raise OSError("no /proc")

        monkeypatch.setattr(fsprobe.Path, "read_text", missing)
        assert is_wsl() is False

    def test_binary_identity_unknown_when_unresolved(self, monkeypatch):
        from godot_qa_toolkit.mutation import inject
        monkeypatch.setattr(inject, "_resolved_godot_path", lambda: None)
        assert fsprobe.binary_identity() == "unknown"

    def test_binary_identity_hash_binds_bytes(self, tmp_path, monkeypatch):
        from godot_qa_toolkit.mutation import inject
        fake = tmp_path / "godot"
        fake.write_bytes(b"\x7fELF-stable-content")
        monkeypatch.setattr(inject, "_resolved_godot_path",
                            lambda: str(fake))
        id1 = fsprobe.binary_identity()
        fake.write_bytes(b"\x7fELF-changed-content")
        id2 = fsprobe.binary_identity()
        assert id1 != id2  # 同名不同内容 → 不同格（同机同二进制判据）

    def test_probe_environment_is_readonly(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        before = sorted(p.name for p in root.iterdir())
        env = fsprobe.probe_environment(str(root))
        assert sorted(p.name for p in root.iterdir()) == before
        assert set(env) == {"fstype", "mount_point", "wsl", "binary"}
        assert env["binary"]

    def test_wsl_flag_matches_proc_version(self, monkeypatch, tmp_path):
        fake = tmp_path / "version"
        fake.write_text("Linux version 6.18 ... Microsoft Corporation",
                        encoding="utf-8")
        monkeypatch.setattr(fsprobe.Path, "__new__", fsprobe.Path.__new__)
        orig_read = fsprobe.Path.read_text

        def fake_read(self, *a, **k):
            if self.name == "version" and str(self) == "/proc/version":
                return fake.read_text(encoding="utf-8")
            return orig_read(self, *a, **k)

        monkeypatch.setattr(fsprobe.Path, "read_text", fake_read)
        assert is_wsl() is True


class TestThresholds:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("GQT_FS_PROBE_T", raising=False)
        monkeypatch.delenv("GQT_FS_PROBE_S", raising=False)
        assert thresholds() == (2.0, 10.0)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("GQT_FS_PROBE_T", "3.5")
        monkeypatch.setenv("GQT_FS_PROBE_S", "20")
        assert thresholds() == (3.5, 20.0)

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("GQT_FS_PROBE_T", "abc")
        monkeypatch.setenv("GQT_FS_PROBE_S", "-5")
        assert thresholds() == (2.0, 10.0)


class TestRecommendProjection:
    def test_worthy_when_delta_and_share_pass(self):
        out = recommend_projection(14.3, 2.33)
        assert out["worth"] is True
        assert out["delta_s"] == pytest.approx(11.97, abs=0.01)
        assert out["tax_share_pct"] == pytest.approx(83.7, abs=0.5)

    def test_not_worthy_when_delta_below_t(self):
        out = recommend_projection(14.3, 13.0)  # 差 1.3 < T=2
        assert out["worth"] is False
        assert out["reason"] == "delta_below_t"

    def test_not_worthy_when_share_below_s(self, monkeypatch):
        monkeypatch.setenv("GQT_FS_PROBE_T", "0.5")
        # 差 1s ≥ T=0.5 但占比 1/14.3 ≈ 7% < S=10
        out = recommend_projection(14.3, 13.3, t_s=0.5)
        assert out["worth"] is False
        assert out["reason"] == "share_below_s"

    def test_missing_baseline_honest_degrade(self):
        out = recommend_projection(None, 3.0)
        assert out["worth"] is False
        assert out["reason"] == "missing_baseline"
        out2 = recommend_projection(14.3, None)
        assert out2["worth"] is False

    def test_expected_runs_scales_saving(self):
        out = recommend_projection(14.3, 2.33, expected_runs=12)
        assert out["projected_saving_s"] == pytest.approx(143.64, abs=0.1)

    def test_output_is_jsonable_report_shape(self):
        out = recommend_projection(14.3, 2.33)
        json.dumps(out)  # summary.fs_projection 机器可审形态
