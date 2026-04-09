import os
import pytest
import yaml
from unittest.mock import patch, mock_open
from conftest import SyncConfig

# ── Fixtures ──

@pytest.fixture
def valid_yaml(tmp_path):
    local = tmp_path / "local"; local.mkdir()
    icloud = tmp_path / "icloud"; icloud.mkdir()
    history = tmp_path / "history"; history.mkdir()
    logs = tmp_path / "logs"; logs.mkdir()
    data = {
        "paths": {
            "local_vault": str(local),
            "icloud_vault": str(icloud),
            "history_dir": str(history),
            "logs_dir": str(logs),
        },
        "sync": {
            "run_continuously": True,
            "user_interface": False,
            "check_icloud_status": True,
            "poll_interval": 5,
            "stability_window": 3,
            "check_icloud_status": True,
        },
        "logging": {
            "console_level": "verbose"
        },
        "ignore": {
            "patterns": ["*.tmp"],
            "dirs": [".trash"],
            "files": [".ds_store"]
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(data))
    return path, data, tmp_path

# ── from_yaml ──

class TestFromYaml:
    def test_loads_valid_yaml(self, valid_yaml):
        path, data, tmp = valid_yaml
        cfg = SyncConfig.from_yaml(str(path))
        assert cfg.poll_interval == 5
        assert cfg.stability_window == 3
        assert cfg.console_level == "verbose"
        assert cfg.user_interface is False
        assert cfg.check_icloud_status is True
        assert "*.tmp" in cfg.ignore_patterns

    def test_defaults_for_missing_sections(self, tmp_path):
        path = tmp_path / "empty.yaml"
        local  = tmp_path / "l"; local.mkdir()
        icloud = tmp_path / "c"; icloud.mkdir()
        path.write_text(yaml.dump({"paths": {"local_vault": str(local), "icloud_vault": str(icloud)}}))
        cfg = SyncConfig.from_yaml(str(path))
        assert cfg.poll_interval == 2
        assert cfg.console_level == "normal"

    def test_check_icloud_status_loaded(self, valid_yaml):
        path, _, _ = valid_yaml
        cfg = SyncConfig.from_yaml(str(path))
        assert cfg.check_icloud_status is True

    def test_check_icloud_status_default_true(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(yaml.dump({"sync": {}, "paths": {}}))
        cfg = SyncConfig.from_yaml(str(path))
        assert cfg.check_icloud_status is True

    def test_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SyncConfig.from_yaml(str(tmp_path / "nonexistent.yaml"))

    def test_raises_invalid_yaml(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("key: [unclosed")
        with pytest.raises(ValueError, match="invalid YAML"):
            SyncConfig.from_yaml(str(path))

    def test_handles_empty_yaml(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("")
        cfg = SyncConfig.from_yaml(str(path))
        assert isinstance(cfg, SyncConfig)
        assert cfg.local_vault == ""

    def test_run_continuously_default(self, tmp_path):
        path = tmp_path / "c.yaml"; path.write_text("{}")
        cfg = SyncConfig.from_yaml(str(path))
        assert cfg.run_continuously is True

# ── validate ──

class TestValidate:
    def test_valid_config_no_errors(self, cfg):
        errors = cfg.validate()
        critical = [e for e in errors if e[1] == "critical"]
        assert not critical

    def test_empty_local_vault_is_critical(self, cfg):
        cfg.local_vault = ""
        errors = cfg.validate()
        codes = [e[0] for e in errors]
        assert "path_not_set" in codes
        severities = {e[0]: e[1] for e in errors}
        assert severities["path_not_set"] == "critical"

    def test_empty_icloud_vault_is_critical(self, cfg):
        cfg.icloud_vault = ""
        errors = cfg.validate()
        assert any(e[0] == "path_not_set" for e in errors)

    def test_nonexistent_local_vault_is_critical(self, cfg, tmp_path):
        cfg.local_vault = str(tmp_path / "ghost")
        errors = cfg.validate()
        assert any(e[0] == "path_does_not_exist" for e in errors)

    def test_same_local_and_icloud_is_critical(self, cfg):
        cfg.icloud_vault = cfg.local_vault
        errors = cfg.validate()
        assert any(e[0] == "same_paths" for e in errors)

    def test_history_inside_local_vault_is_critical(self, cfg):
        cfg.history_dir = os.path.join(cfg.local_vault, "history")
        os.makedirs(cfg.history_dir, exist_ok=True)
        errors = cfg.validate()
        assert any(e[0] == "history_dir_inside_vault" for e in errors)

    def test_history_inside_icloud_vault_is_critical(self, cfg):
        cfg.history_dir = os.path.join(cfg.icloud_vault, "hist")
        os.makedirs(cfg.history_dir, exist_ok=True)
        errors = cfg.validate()
        assert any(e[0] == "history_dir_inside_vault" for e in errors)

    def test_missing_history_dir_is_warn(self, cfg, tmp_path):
        cfg.history_dir = str(tmp_path / "nonexistent_hist")
        errors = cfg.validate()
        warns = [e for e in errors if e[1] == "warn"]
        assert any(e[0] == "dir_missing" for e in warns)

    def test_suspicious_pattern_is_warn(self, cfg):
        cfg.ignore_patterns = ["//bad"]
        errors = cfg.validate()
        assert any(e[0] == "suspicious_ignore_pattern" for e in errors)

    def test_returns_empty_for_perfect_config(self, cfg):
        assert cfg.validate() == []

# ── is_ignored ──

class TestIsIgnored:
    def test_exact_match(self, cfg):
        cfg.ignore_patterns = ["notes/private.md"]
        assert cfg.is_ignored("notes/private.md") is True

    def test_glob_wildcard(self, cfg):
        cfg.ignore_patterns = ["*.canvas"]
        assert cfg.is_ignored("diagram.canvas") is True
        assert cfg.is_ignored("diagram.md") is False

    def test_dir_prefix(self, cfg):
        cfg.ignore_patterns = ["Templates"]
        assert cfg.is_ignored("Templates/hello.md") is True
        assert cfg.is_ignored("other/hello.md") is False

    def test_no_match(self, cfg):
        cfg.ignore_patterns = ["*.tmp"]
        assert cfg.is_ignored("notes.md") is False

    def test_backslash_normalized(self, cfg):
        cfg.ignore_patterns = ["notes/private.md"]
        assert cfg.is_ignored(r"notes\private.md") is True

    def test_case_insensitive(self, cfg):
        cfg.ignore_patterns = ["*.TMP"]
        assert cfg.is_ignored("file.tmp") is True

    def test_empty_patterns(self, cfg):
        cfg.ignore_patterns = []
        assert cfg.is_ignored("anything.md") is False

# ── disp ──

class TestDisp:
    def test_short_path_unchanged(self, cfg):
        cfg.shorter_paths = True
        assert cfg.disp("note.md") == "note.md"

    def test_absolute_local_path_becomes_relative(self, cfg):
        cfg.shorter_paths = True
        p = os.path.join(cfg.local_vault, "note.md")
        result = cfg.disp(p)
        assert "note.md" in result
        assert cfg.local_vault not in result

    def test_absolute_icloud_path_becomes_relative(self, cfg):
        cfg.shorter_paths = True
        p = os.path.join(cfg.icloud_vault, "note.md")
        result = cfg.disp(p)
        assert "note.md" in result
        assert cfg.icloud_vault not in result

    def test_long_path_truncated(self, cfg):
        cfg.shorter_paths = True
        cfg.max_display_length = 20
        long_path = "a/b/c/d/e/very_long_name.md"
        result = cfg.disp(long_path)
        assert len(result) <= cfg.max_display_length + 3

    def test_shorter_paths_false(self, cfg):
        cfg.shorter_paths = False
        p = "/some/absolute/path.md"
        assert cfg.disp(p) == p

# ── min_seed_size ──

class TestMinSeedSize:
    def test_obsidian_settings_allow_1_byte(self, cfg):
        cfg.tiny_threshold = 8
        assert cfg.min_seed_size(".obsidian/app.json") == 1

    def test_regular_file_uses_tiny_threshold(self, cfg):
        cfg.tiny_threshold = 8
        assert cfg.min_seed_size("notes/hello.md") == 8

    def test_nested_obsidian_path(self, cfg):
        cfg.tiny_threshold = 8
        assert cfg.min_seed_size(".obsidian/plugins/x/data.json") == 1

# ── state_file_path ──

class TestStateFilePath:
    def test_returns_path_in_logs_dir(self, cfg):
        result = cfg.state_file_path
        assert result.startswith(cfg.logs_dir)
        assert result.endswith("sync_state.json")