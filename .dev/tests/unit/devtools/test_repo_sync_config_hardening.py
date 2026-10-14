"""Hardening tests (SEE-1406 ③ hardener): config / logging / state resolution.

Mutation-survivor killers: these tests exist because token-level mutants in
_resolve_repo_path, load_config, setup_logging, and write_state previously
survived the full suite — the sentinel error branch, relative-path derivation,
quiet default, and idempotent directory creation had no behavioral coverage.
"""

import json
import logging
import os

import pytest

from repo_sync import load_config, setup_logging, write_state


class TestResolveRepoPathSentinel:
    def test_sentinel_with_env_resolves_config_paths(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GQT_REPO_ROOT", str(tmp_path))
        config_file = tmp_path / "repo_sync_config.json"
        config_file.write_text(
            json.dumps(
                {
                    "repo_path": "__GQT_REPO_ROOT__",
                    "remote": "origin",
                    "branch": "master",
                    "log_dir": "logs",
                    "state_file": "state/state.json",
                }
            )
        )
        cfg = load_config(config_file)
        assert cfg["repo_path"] == str(tmp_path)
        assert cfg["log_dir"] == str(tmp_path / "logs")
        assert cfg["state_file"] == str(tmp_path / "state" / "state.json")

    def test_sentinel_with_nested_repo_path_matches_derived_paths(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GQT_REPO_ROOT", str(tmp_path / "root"))
        config_file = tmp_path / "repo_sync_config.json"
        config_file.write_text(
            json.dumps(
                {
                    "repo_path": "__GQT_REPO_ROOT__/nested",
                    "remote": "origin",
                    "gitlink_forward_enabled": False,
                }
            )
        )
        cfg = load_config(config_file)
        assert cfg["repo_path"] == str(tmp_path / "root" / "nested")

    def test_sentinel_without_env_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GQT_REPO_ROOT", raising=False)
        config_file = tmp_path / "repo_sync_config.json"
        config_file.write_text(
            json.dumps(
                {
                    "repo_path": "__GQT_REPO_ROOT__",
                    "remote": "origin",
                }
            )
        )
        with pytest.raises(RuntimeError, match="GQT_REPO_ROOT"):
            load_config(config_file)

    def test_absolute_paths_stay_absolute(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GQT_REPO_ROOT", str(tmp_path))
        abs_log = tmp_path / "elsewhere-logs"
        config_file = tmp_path / "repo_sync_config.json"
        config_file.write_text(
            json.dumps(
                {
                    "repo_path": "__GQT_REPO_ROOT__",
                    "remote": "origin",
                    "log_dir": str(abs_log),
                    "state_file": str(tmp_path / "state" / "state.json"),
                }
            )
        )
        cfg = load_config(config_file)
        assert cfg["log_dir"] == str(abs_log)
        assert cfg["state_file"] == str(tmp_path / "state" / "state.json")

    def test_missing_optional_keys_stay_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GQT_REPO_ROOT", str(tmp_path))
        config_file = tmp_path / "repo_sync_config.json"
        config_file.write_text(
            json.dumps(
                {
                    "repo_path": "__GQT_REPO_ROOT__",
                    "remote": "origin",
                }
            )
        )
        cfg = load_config(config_file)
        assert "log_dir" not in cfg
        assert "state_file" not in cfg

    def test_non_sentinel_repo_path_expands_user(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GQT_REPO_ROOT", raising=False)
        config_file = tmp_path / "repo_sync_config.json"
        config_file.write_text(json.dumps({"repo_path": "~", "remote": "origin"}))
        cfg = load_config(config_file)
        assert cfg["repo_path"] == os.path.expanduser("~")


class TestSetupLoggingIdempotent:
    def _make_config(self, tmp_path, log_dir_name="logs"):
        return {"log_dir": str(tmp_path / log_dir_name)}

    def test_creates_missing_nested_log_dir(self, tmp_path):
        config = self._make_config(tmp_path, log_dir_name="a/b/logs")
        setup_logging(config, quiet=True)
        assert (tmp_path / "a" / "b" / "logs").is_dir()

    def test_existing_log_dir_no_raise_and_keeps_handler(self, tmp_path):
        config = self._make_config(tmp_path)
        first = setup_logging(config, quiet=True)
        assert first.name == "repo_sync"
        # Second call over an existing log dir must not raise FileExistsError
        second = setup_logging(config, quiet=True)
        file_handlers = [
            h for h in second.handlers if isinstance(h, logging.FileHandler)
        ]
        assert file_handlers, "file handler must be present after re-setup"

    def test_default_creates_stream_handler_on_stdout(self, tmp_path):
        config = self._make_config(tmp_path)
        logger = setup_logging(config)  # quiet default: False
        streams = [
            getattr(h, "stream", None) for h in logger.handlers
        ]
        assert any(s is os.sys.stdout for s in streams), (
            "default (quiet=False) must attach a StreamHandler on sys.stdout"
        )

    def test_quiet_true_has_no_stream_handler(self, tmp_path):
        config = self._make_config(tmp_path)
        logger = setup_logging(config, quiet=True)
        streams = [getattr(h, "stream", None) for h in logger.handlers]
        assert all(s is not os.sys.stdout for s in streams)


class TestWriteStateIdempotent:
    def _config(self, state_file):
        return {"state_file": str(state_file)}

    def test_creates_missing_nested_state_dir(self, tmp_path):
        state_file = tmp_path / "missing" / "nested" / "state.json"
        write_state(self._config(state_file), {"state": "SYNCED"})
        on_disk = json.loads(state_file.read_text(encoding="utf-8"))
        assert on_disk["state"] == "SYNCED"
        assert "timestamp" in on_disk

    def test_existing_state_dir_no_raise_and_overwrites(self, tmp_path):
        state_file = tmp_path / "state.json"
        write_state(self._config(state_file), {"state": "SYNCED"})
        # Second write to the same (existing) dir must not raise FileExistsError
        write_state(self._config(state_file), {"state": "LOCAL_AHEAD"})
        on_disk = json.loads(state_file.read_text(encoding="utf-8"))
        assert on_disk["state"] == "LOCAL_AHEAD"
        assert "timestamp" in on_disk
