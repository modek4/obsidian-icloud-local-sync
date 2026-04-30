import os
import asyncio
import pytest
import time
from unittest.mock import patch, MagicMock
from conftest import SyncLogger, strip_ansi, colored, LEVEL_MAP
from colorama import Fore, Style

# ── Helpers ──

class TestHelpers:
    def test_strip_ansi_removes_codes(self):
        raw = "\x1b[32mGREEN\x1b[0m text"
        assert strip_ansi(raw) == "GREEN text"

    def test_strip_ansi_plain_text_unchanged(self):
        assert strip_ansi("hello world") == "hello world"

    def test_strip_ansi_empty_string(self):
        assert strip_ansi("") == ""

    def test_colored_wraps_in_codes(self):
        result = colored("hello", Fore.RED)
        assert "hello" in result
        assert Style.RESET_ALL in result

    def test_level_map_ordering(self):
        assert LEVEL_MAP["quiet"] < LEVEL_MAP["normal"]
        assert LEVEL_MAP["normal"] < LEVEL_MAP["verbose"]

# ── console_event ──

class TestConsoleEvent:
    def _logger(self, cfg, level="normal"):
        cfg.console_level = level
        return SyncLogger(cfg)

    def test_important_always_prints(self, cfg, capsys):
        cfg.console_level = "quiet"
        log = SyncLogger(cfg)
        log.console_event("🔵", Fore.CYAN, "TEST", "msg", level="important")
        assert "msg" in capsys.readouterr().out

    def test_quiet_suppresses_normal(self, cfg, capsys):
        cfg.console_level = "quiet"
        log = SyncLogger(cfg)
        log.console_event("🔵", Fore.CYAN, "TEST", "msg", level="normal")
        assert capsys.readouterr().out == ""

    def test_quiet_suppresses_verbose(self, cfg, capsys):
        cfg.console_level = "quiet"
        log = SyncLogger(cfg)
        log.console_event("🔵", Fore.CYAN, "TEST", "msg", level="verbose")
        assert capsys.readouterr().out == ""

    def test_normal_shows_normal_events(self, cfg, capsys):
        cfg.console_level = "normal"
        log = SyncLogger(cfg)
        log.console_event("🔵", Fore.CYAN, "TEST", "msg", level="normal")
        assert "msg" in capsys.readouterr().out

    def test_normal_suppresses_verbose(self, cfg, capsys):
        cfg.console_level = "normal"
        log = SyncLogger(cfg)
        log.console_event("🔵", Fore.CYAN, "TEST", "msg", level="verbose")
        assert capsys.readouterr().out == ""

    def test_verbose_shows_all(self, cfg, capsys):
        cfg.console_level = "verbose"
        log = SyncLogger(cfg)
        log.console_event("🔵", Fore.CYAN, "TEST", "msg", level="verbose")
        assert "msg" in capsys.readouterr().out

# ── write_to_file, flush ──

class TestFileLogging:
    def test_no_log_file_skips_buffering(self, cfg):
        log = SyncLogger(cfg)
        log.log_file = None
        log.write_to_file("INFO", "hello")
        assert log._buffer == []

    def test_adds_to_buffer(self, cfg):
        log = SyncLogger(cfg)
        log.log_file = str(cfg.logs_dir) + "/test.log"
        log.write_to_file("INFO", "hello")
        assert len(log._buffer) == 1
        assert "hello" in log._buffer[0]

    def test_strips_ansi_in_buffer(self, cfg):
        log = SyncLogger(cfg)
        log.log_file = str(cfg.logs_dir) + "/test.log"
        log.write_to_file("INFO", colored("hello", Fore.GREEN))
        assert "\x1b" not in log._buffer[0]

    def test_auto_flush_at_20(self, cfg, tmp_path):
        log = SyncLogger(cfg)
        log_path = str(tmp_path / "autoflush.log")
        log.log_file = log_path
        for i in range(20):
            log.write_to_file("X", f"msg{i}")
        assert log._buffer == []
        assert os.path.exists(log_path)

    def test_flush_writes_to_file(self, cfg, tmp_path):
        log = SyncLogger(cfg)
        log_path = str(tmp_path / "flush.log")
        log.log_file = log_path
        log._buffer = ["[ts] [INFO] hello\n"]
        log.flush()
        content = open(log_path).read()
        assert "hello" in content
        assert log._buffer == []

    def test_flush_empty_buffer_is_noop(self, cfg, tmp_path):
        log = SyncLogger(cfg)
        log.log_file = str(tmp_path / "noop.log")
        log._buffer = []
        log.flush()
        assert not os.path.exists(str(tmp_path / "noop.log"))

    def test_flush_no_log_file_is_noop(self, cfg):
        log = SyncLogger(cfg)
        log.log_file = None
        log._buffer = ["data"]
        log.flush()
        assert log._buffer == ["data"]

# ── Log Methods ──

class TestLogMethods:
    @pytest.fixture
    def log(self, cfg):
        l = SyncLogger(cfg)
        l.log_file = str(cfg.logs_dir) + "/test.log"
        return l

    def test_info_calls_console_and_file(self, log):
        with patch.object(log, "console_event") as ce, \
             patch.object(log, "write_to_file") as wf:
            log.info("INFO", "test msg", level="verbose")
            ce.assert_called_once()
            wf.assert_called_once_with("INFO", "test msg")

    def test_warn_calls_console_and_file(self, log):
        with patch.object(log, "console_event") as ce, \
             patch.object(log, "write_to_file") as wf:
            log.warn("WARN", "warning")
            ce.assert_called_once()
            wf.assert_called_once()

    def test_error_calls_console_and_file(self, log):
        with patch.object(log, "console_event") as ce, \
             patch.object(log, "write_to_file") as wf:
            log.error("ERR", "error msg")
            ce.assert_called_once()
            wf.assert_called_once()

    def test_success_calls_console_and_file(self, log):
        with patch.object(log, "console_event") as ce, \
             patch.object(log, "write_to_file") as wf:
            log.success("OK", "done")
            ce.assert_called_once()
            wf.assert_called_once()

    def test_error_critical_exits(self, log):
        with patch.object(log, "flush"), \
             pytest.raises(SystemExit) as exc_info:
            log.error("CRITICAL", "fatal", critical=True)
        assert exc_info.value.code == 1

    def test_custom_verbose_uses_full_msg(self, log, cfg, capsys):
        cfg.console_level = "verbose"
        log.custom(["→", "🔵"], [Fore.GREEN, Fore.CYAN], "PUSH", "Full detailed message", "short.md", level="verbose")
        out = capsys.readouterr().out
        assert "Full detailed message" in out

    def test_custom_normal_uses_short_path(self, log, cfg, capsys):
        cfg.console_level = "normal"
        log.custom(["→", "🔵"], [Fore.GREEN, Fore.CYAN], "PUSH", "Full detailed message", "short.md", level="normal")
        out = capsys.readouterr().out
        assert "short.md" in out
        assert "Full detailed message" not in out

# ── init_log_file ──

class TestInitLogFile:
    def test_sets_log_file_with_timestamp(self, cfg):
        log = SyncLogger(cfg)
        log.init_log_file()
        assert log.log_file is not None
        assert "sync_" in log.log_file
        assert log.log_file.endswith(".log")
        assert log.log_file.startswith(cfg.logs_dir)
        assert os.path.exists(os.path.dirname(log.log_file))

# ── list_log_files ──

class TestListLogFiles:
    def test_returns_sorted_by_mtime(self, cfg, tmp_path):
        log = SyncLogger(cfg)
        a = tmp_path / "sync_a.log"; a.write_text("a"); time.sleep(0.01)
        b = tmp_path / "sync_b.log"; b.write_text("b")
        result = log.list_log_files(str(tmp_path))
        assert result[-1].endswith("sync_b.log")

    def test_returns_empty_for_missing_dir(self, cfg, tmp_path):
        log = SyncLogger(cfg)
        result = log.list_log_files(str(tmp_path / "nonexistent"))
        assert result == []

    def test_filters_only_log_files(self, cfg, tmp_path):
        (tmp_path / "file.txt").write_text("x")
        (tmp_path / "sync.log").write_text("y")
        log = SyncLogger(cfg)
        result = log.list_log_files(str(tmp_path))
        assert all(f.endswith(".log") for f in result)

# ── cleanup_old_logs ──

class TestCleanupOldLogs:
    @pytest.mark.asyncio
    async def test_removes_old_logs_beyond_retention(self, cfg, tmp_path):
        cfg.logs_dir = str(tmp_path)
        cfg.log_retention = 2
        for i in range(4):
            (tmp_path / f"sync_{i:03d}.log").write_text(f"log{i}")
            time.sleep(0.01)
        log = SyncLogger(cfg)
        await log.cleanup_old_logs()
        remaining = list(tmp_path.glob("*.log"))
        assert len(remaining) == 2

    @pytest.mark.asyncio
    async def test_keeps_at_least_1_log(self, cfg, tmp_path):
        cfg.logs_dir = str(tmp_path)
        cfg.log_retention = 0
        (tmp_path / "sync_1.log").write_text("x")
        log = SyncLogger(cfg)
        await log.cleanup_old_logs()
        remaining = list(tmp_path.glob("*.log"))
        assert len(remaining) >= 1

    @pytest.mark.asyncio
    async def test_no_error_when_no_logs(self, cfg, tmp_path):
        cfg.logs_dir = str(tmp_path)
        log = SyncLogger(cfg)
        await log.cleanup_old_logs()