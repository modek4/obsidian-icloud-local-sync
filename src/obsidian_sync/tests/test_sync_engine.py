import os
import time
import asyncio
import shutil
import hashlib
import pytest
from obsidian_sync.disk_io import DiskIO
from unittest.mock import patch, MagicMock, AsyncMock, call
from conftest import SyncEngine, FileHasher, ICloudSyncState, ICloudStatusChecker, DiskIO

# ── Fixtures ──

@pytest.fixture
def eng(cfg, mock_log, tmp_path):
    with patch("platform.system", return_value="Windows"):
        real_io = DiskIO(cfg, mock_log)
    h   = FileHasher(cfg, mock_log)
    dup = MagicMock()
    return SyncEngine(cfg, mock_log, h, real_io, dup)

def _write(path: str, content: str = "content"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path

def _local(cfg, rel): return os.path.join(cfg.local_vault,  rel)
def _icloud(cfg, rel): return os.path.join(cfg.icloud_vault, rel)
def _history(cfg, rel): return os.path.join(cfg.history_dir,  rel)

# ── Init ──

class TestInit:
    def test_checker_created_when_enabled(self, cfg, mock_log):
        cfg.check_icloud_status = True
        with patch("platform.system", return_value="Windows"):
            io = DiskIO(cfg, mock_log)
            eng = SyncEngine(cfg, mock_log, MagicMock(), io, MagicMock())
        assert eng.icloud_checker is not None

    def test_checker_none_when_disabled(self, cfg, mock_log):
        cfg.check_icloud_status = False
        with patch("platform.system", return_value="Windows"):
            io = DiskIO(cfg, mock_log)
            eng = SyncEngine(cfg, mock_log, MagicMock(), io, MagicMock())
        assert eng.icloud_checker is None

# ── gather_rel_paths ──

class TestGatherRelPaths:
    @pytest.mark.asyncio
    async def test_collects_files_from_local(self, eng, cfg):
        _write(_local(cfg, "a.md"))
        _write(_local(cfg, "b.md"))
        paths = eng.gather_rel_paths()
        assert "a.md" in paths
        assert "b.md" in paths

    @pytest.mark.asyncio
    async def test_collects_files_from_icloud(self, eng, cfg):
        _write(_icloud(cfg, "c.md"))
        paths = eng.gather_rel_paths()
        assert "c.md" in paths

    @pytest.mark.asyncio
    async def test_deduplicates_across_vaults(self, eng, cfg):
        _write(_local(cfg, "same.md"))
        _write(_icloud(cfg, "same.md"))
        paths = eng.gather_rel_paths()
        assert "same.md" in paths

    @pytest.mark.asyncio
    async def test_filters_tmp_files(self, eng, cfg):
        _write(_local(cfg, "draft.tmp"))
        paths = eng.gather_rel_paths()
        assert "draft.tmp" not in paths

    @pytest.mark.asyncio
    async def test_filters_dotunderscore_files(self, eng, cfg):
        _write(_local(cfg, "._something.md"))
        paths = eng.gather_rel_paths()
        assert all("._" not in p for p in paths)

    @pytest.mark.asyncio
    async def test_filters_page_preview(self, eng, cfg):
        _write(_local(cfg, "page-preview.md"))
        paths = eng.gather_rel_paths()
        assert "page-preview.md" not in paths

    @pytest.mark.asyncio
    async def test_filters_ignored_patterns(self, eng, cfg):
        cfg.ignore_patterns = ["private/*.md"]
        os.makedirs(os.path.join(cfg.local_vault, "private"), exist_ok=True)
        _write(_local(cfg, "private/secret.md"))
        paths = eng.gather_rel_paths()
        assert os.path.normpath("private/secret.md") not in paths

    @pytest.mark.asyncio
    async def test_filters_ignored_dirs(self, eng, cfg):
        cfg.ignored_dirs = [".git"]
        os.makedirs(os.path.join(cfg.local_vault, ".git"), exist_ok=True)
        _write(_local(cfg, ".git/HEAD"))
        paths = eng.gather_rel_paths()
        assert ".git/HEAD" not in paths

    @pytest.mark.asyncio
    async def test_handles_empty_vaults(self, eng, cfg):
        paths = eng.gather_rel_paths()
        assert isinstance(paths, (list, set))
        assert len(paths) == 0

# ── sync_file L/C/H ──

class TestSyncFileStates:
    @pytest.mark.asyncio
    async def test_all_missing_does_nothing(self, eng, cfg):
        await eng.sync_file("ghost.md")
        eng.log.success.assert_not_called()
        eng.log.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_history_cleans_it_up(self, eng, cfg):
        _write(_history(cfg, "orphan.md"))
        await eng.sync_file("orphan.md")
        assert not os.path.exists(_history(cfg, "orphan.md"))

    @pytest.mark.asyncio
    async def test_local_only_pushes_to_icloud(self, eng, cfg):
        _write(_local(cfg, "new.md"), "fresh content")
        await eng.sync_file("new.md")
        assert os.path.exists(_icloud(cfg, "new.md"))
        assert os.path.exists(_history(cfg, "new.md"))

    @pytest.mark.asyncio
    async def test_icloud_only_pulls_to_local(self, eng, cfg):
        _write(_icloud(cfg, "remote.md"), "from cloud")
        await eng.sync_file("remote.md")
        assert os.path.exists(_local(cfg, "remote.md"))
        assert os.path.exists(_history(cfg, "remote.md"))

    @pytest.mark.asyncio
    async def test_identical_l_and_c_no_h_seeds_history(self, eng, cfg):
        content = "same content"
        _write(_local(cfg, "sync.md"), content)
        _write(_icloud(cfg, "sync.md"), content)
        await eng.sync_file("sync.md")
        assert os.path.exists(_history(cfg, "sync.md"))

    @pytest.mark.asyncio
    async def test_identical_l_c_h_skips(self, eng, cfg):
        content = "stable content"
        for f in (_local(cfg, "stable.md"), _icloud(cfg, "stable.md"), _history(cfg, "stable.md")):
            _write(f, content)
        h = hashlib.sha256(content.encode()).hexdigest()
        eng.hasher.state["stable.md"] = {
            "L": {"mtime": os.path.getmtime(_local(cfg, "stable.md")), "size": os.path.getsize(_local(cfg, "stable.md")), "hash": h},
            "C": {"mtime": os.path.getmtime(_icloud(cfg, "stable.md")), "size": os.path.getsize(_icloud(cfg, "stable.md")), "hash": h},
            "H": {"mtime": os.path.getmtime(_history(cfg, "stable.md")), "size": os.path.getsize(_history(cfg, "stable.md")), "hash": h},
        }
        eng.log.success.reset_mock()
        await eng.sync_file("stable.md")
        content_icloud = open(_icloud(cfg, "stable.md")).read()
        content_local = open(_local(cfg, "stable.md")).read()
        assert content_icloud == "stable content"
        assert content_local == "stable content"

    @pytest.mark.asyncio
    async def test_local_changed_pushes(self, eng, cfg):
        old = "old content"
        new = "new local content"
        for f in (_local(cfg, "mod.md"), _icloud(cfg, "mod.md"), _history(cfg, "mod.md")):
            _write(f, old)
        h = hashlib.sha256(old.encode()).hexdigest()
        old_mtime = os.path.getmtime(_history(cfg, "mod.md"))
        eng.hasher.state["mod.md"] = {
            "L": {"mtime": old_mtime, "size": len(old), "hash": h},
            "C": {"mtime": old_mtime, "size": len(old), "hash": h},
            "H": {"mtime": old_mtime, "size": len(old), "hash": h},
        }
        time.sleep(0.01)
        _write(_local(cfg, "mod.md"), new)
        await eng.sync_file("mod.md")
        assert open(_icloud(cfg, "mod.md")).read() == new

    @pytest.mark.asyncio
    async def test_icloud_changed_pulls(self, eng, cfg):
        old = "shared base"
        new_cloud = "new icloud content"
        for f in (_local(cfg, "pull.md"), _icloud(cfg, "pull.md"), _history(cfg, "pull.md")):
            _write(f, old)
        h = hashlib.sha256(old.encode()).hexdigest()
        old_mtime = os.path.getmtime(_local(cfg, "pull.md"))
        eng.hasher.state["pull.md"] = {
            "L": {"mtime": old_mtime, "size": len(old), "hash": h},
            "C": {"mtime": old_mtime, "size": len(old), "hash": h},
            "H": {"mtime": old_mtime, "size": len(old), "hash": h},
        }
        time.sleep(0.01)
        _write(_icloud(cfg, "pull.md"), new_cloud)
        await eng.sync_file("pull.md")
        assert open(_local(cfg, "pull.md")).read() == new_cloud

    @pytest.mark.asyncio
    async def test_both_changed_conflict_resolves(self, eng, cfg):
        base = "base version"
        local_new = "local changed"
        cloud_new = "cloud changed"
        for f in (_local(cfg, "conflict.md"), _icloud(cfg, "conflict.md"), _history(cfg, "conflict.md")):
            _write(f, base)
        h = hashlib.sha256(base.encode()).hexdigest()
        old_t = os.path.getmtime(_history(cfg, "conflict.md"))
        eng.hasher.state["conflict.md"] = {
            "L": {"mtime": old_t, "size": len(base), "hash": h},
            "C": {"mtime": old_t, "size": len(base), "hash": h},
            "H": {"mtime": old_t, "size": len(base), "hash": h},
        }
        time.sleep(0.01)
        _write(_local(cfg, "conflict.md"), local_new)
        time.sleep(0.01)
        _write(_icloud(cfg, "conflict.md"), cloud_new)
        await eng.sync_file("conflict.md")
        content = open(_local(cfg, "conflict.md")).read()
        assert content == cloud_new

    @pytest.mark.asyncio
    async def test_user_deleted_local_removes_everywhere(self, eng, cfg):
        old = "to delete"
        for f in (_icloud(cfg, "del.md"), _history(cfg, "del.md")):
            _write(f, old)
        h = hashlib.sha256(old.encode()).hexdigest()
        t = os.path.getmtime(_icloud(cfg, "del.md"))
        eng.hasher.state["del.md"] = {
            "C": {"mtime": t, "size": len(old), "hash": h},
            "H": {"mtime": t, "size": len(old), "hash": h},
        }
        await eng.sync_file("del.md")
        assert not os.path.exists(_icloud(cfg, "del.md"))
        assert not os.path.exists(_history(cfg, "del.md"))

    @pytest.mark.asyncio
    async def test_user_deleted_icloud_removes_everywhere(self, eng, cfg):
        old = "to delete"
        for f in (_local(cfg, "del2.md"), _history(cfg, "del2.md")):
            _write(f, old)
        h = hashlib.sha256(old.encode()).hexdigest()
        t = os.path.getmtime(_local(cfg, "del2.md"))
        eng.hasher.state["del2.md"] = {
            "L": {"mtime": t, "size": len(old), "hash": h},
            "H": {"mtime": t, "size": len(old), "hash": h},
        }
        await eng.sync_file("del2.md")
        assert not os.path.exists(_local(cfg, "del2.md"))
        assert not os.path.exists(_history(cfg, "del2.md"))

    @pytest.mark.asyncio
    async def test_no_local_but_icloud_changed_restores(self, eng, cfg):
        old = "shared base"
        new = "cloud updated"
        _write(_history(cfg, "restore.md"), old)
        _write(_icloud(cfg, "restore.md"), new)
        h_old = hashlib.sha256(old.encode()).hexdigest()
        t = os.path.getmtime(_history(cfg, "restore.md"))
        eng.hasher.state["restore.md"] = {
            "H": {"mtime": t, "size": len(old), "hash": h_old},
        }
        await eng.sync_file("restore.md")
        assert os.path.exists(_local(cfg, "restore.md"))

    @pytest.mark.asyncio
    async def test_no_icloud_but_local_changed_pushes(self, eng, cfg):
        old = "shared base"
        new = "local updated"
        _write(_history(cfg, "push2.md"), old)
        _write(_local(cfg, "push2.md"), new)
        h_old = hashlib.sha256(old.encode()).hexdigest()
        t = os.path.getmtime(_history(cfg, "push2.md"))
        eng.hasher.state["push2.md"] = {
            "H": {"mtime": t, "size": len(old), "hash": h_old},
        }
        await eng.sync_file("push2.md")
        assert os.path.exists(_icloud(cfg, "push2.md"))

# ── Cooldown ──

class TestCooldown:
    @pytest.mark.asyncio
    async def test_file_in_cooldown_is_skipped(self, eng, cfg):
        _write(_local(cfg, "cool.md"), "x")
        eng.cooldowns["cool.md"] = time.time() + 9999
        initial_mtime = os.path.getmtime(_local(cfg, "cool.md"))
        await eng.sync_file("cool.md")
        assert not os.path.exists(_icloud(cfg, "cool.md"))

    @pytest.mark.asyncio
    async def test_expired_cooldown_allows_sync(self, eng, cfg):
        _write(_local(cfg, "cool2.md"), "y" * 20)
        eng.cooldowns["cool2.md"] = time.time() - 1
        await eng.sync_file("cool2.md")
        assert os.path.exists(_icloud(cfg, "cool2.md"))

# ── iCloud guard ──

class TestICloudGuard:
    @pytest.mark.asyncio
    async def test_cloud_only_state_defers_sync(self, eng, cfg):
        _write(_local(cfg, "guarded.md"), "x")
        _write(_icloud(cfg, "guarded.md"), "x")

        mock_checker = MagicMock(spec=ICloudStatusChecker)
        mock_checker.detect.return_value = ICloudSyncState.CLOUD_ONLY
        mock_checker.is_safe.return_value = False
        eng.icloud_checker = mock_checker
        cfg.check_icloud_status = True

        await eng.sync_file("guarded.md")
        eng.log.info.assert_called()
        assert open(_icloud(cfg, "guarded.md")).read() == "x"

    @pytest.mark.asyncio
    async def test_local_state_allows_sync(self, eng, cfg):
        _write(_local(cfg, "ready.md"), "ready content")
        _write(_icloud(cfg, "ready.md"), "old content")

        mock_checker = MagicMock(spec=ICloudStatusChecker)
        mock_checker.detect.return_value = ICloudSyncState.LOCAL
        mock_checker.is_safe.return_value = True
        eng.icloud_checker = mock_checker
        cfg.check_icloud_status = True

        _write(_history(cfg, "ready.md"), "old content")
        h = hashlib.sha256(b"old content").hexdigest()
        t = os.path.getmtime(_icloud(cfg, "ready.md"))
        eng.hasher.state["ready.md"] = {
            "C": {"mtime": t, "size": len("old content"), "hash": h},
            "H": {"mtime": t, "size": len("old content"), "hash": h},
        }
        await eng.sync_file("ready.md")
        assert open(_icloud(cfg, "ready.md")).read() == "ready content"

# ── iCloud guard integration ──

class TestICloudGuardIntegration:
    @pytest.mark.asyncio
    async def test_defers_when_unsafe(self, eng, cfg):
        _write(_local(cfg, "guarded.md"), "x" * 50)
        _write(_icloud(cfg, "guarded.md"), "original")
        mock_checker = MagicMock()
        mock_checker.detect.return_value = ICloudSyncState.CLOUD_ONLY
        eng.icloud_checker = mock_checker
        cfg.check_icloud_status = True
        await eng.sync_file("guarded.md")
        assert open(_icloud(cfg, "guarded.md")).read() == "original"

    @pytest.mark.asyncio
    async def test_allows_when_safe(self, eng, cfg):
        _write(_local(cfg, "ready.md"), "updated content")
        _write(_icloud(cfg, "ready.md"), "old content")
        _write(_history(cfg, "ready.md"), "old content")
        h = hashlib.sha256(b"old content").hexdigest()
        t = os.path.getmtime(_icloud(cfg, "ready.md"))
        eng.hasher.state["ready.md"] = {
            "C": {"mtime": t, "size": len("old content"), "hash": h},
            "H": {"mtime": t, "size": len("old content"), "hash": h},
        }
        mock_checker = MagicMock()
        mock_checker.is_safe.return_value = True
        eng.icloud_checker = mock_checker
        cfg.check_icloud_status = True
        await eng.sync_file("ready.md")
        assert open(_icloud(cfg, "ready.md")).read() == "updated content"

    @pytest.mark.asyncio
    async def test_guard_skipped_when_checker_is_none(self, eng, cfg):
        cfg.check_icloud_status = False
        eng.icloud_checker = None
        _write(_local(cfg, "bypass.md"), "x" * 50)
        await eng.sync_file("bypass.md")
        assert os.path.exists(_icloud(cfg, "bypass.md"))

# ── Tiny file guard ──

class TestTinyFiles:
    @pytest.mark.asyncio
    async def test_tiny_file_skipped_from_local(self, eng, cfg):
        cfg.tiny_threshold = 10
        _write(_local(cfg, "tiny.md"), "hi")
        await eng.sync_file("tiny.md")
        assert not os.path.exists(_icloud(cfg, "tiny.md"))

    @pytest.mark.asyncio
    async def test_obsidian_settings_not_skipped(self, eng, cfg):
        cfg.tiny_threshold = 10
        os.makedirs(os.path.join(cfg.local_vault, ".obsidian"), exist_ok=True)
        _write(os.path.join(cfg.local_vault, ".obsidian", "app.json"), "{}")
        await eng.sync_file(".obsidian/app.json")
        assert os.path.exists(os.path.join(cfg.icloud_vault, ".obsidian", "app.json"))


# ── push_to_icloud / restore_from_icloud ──

class TestPushRestore:
    @pytest.mark.asyncio
    async def test_push_copies_to_icloud_and_history(self, eng, cfg):
        _write(_local(cfg, "push.md"), "push content")
        await eng.push_to_icloud("push.md")
        assert os.path.exists(_icloud(cfg, "push.md"))
        assert os.path.exists(_history(cfg, "push.md"))

    @pytest.mark.asyncio
    async def test_push_sets_cooldown(self, eng, cfg):
        _write(_local(cfg, "cd.md"), "x")
        await eng.push_to_icloud("cd.md")
        assert "cd.md" in eng.cooldowns

    @pytest.mark.asyncio
    async def test_restore_copies_to_local_and_history(self, eng, cfg):
        _write(_icloud(cfg, "restore.md"), "cloud content")
        await eng.restore_from_icloud("restore.md")
        assert os.path.exists(_local(cfg, "restore.md"))
        assert os.path.exists(_history(cfg, "restore.md"))

    @pytest.mark.asyncio
    async def test_restore_sets_cooldown(self, eng, cfg):
        _write(_icloud(cfg, "rcd.md"), "x")
        await eng.restore_from_icloud("rcd.md")
        assert "rcd.md" in eng.cooldowns

# ── sync_wrapper ──

class TestSyncWrapper:
    @pytest.mark.asyncio
    async def test_exception_is_caught_and_logged(self, eng, cfg):
        eng.io.async_copy = AsyncMock(side_effect=RuntimeError("disk full"))
        _write(_local(cfg, "broken.md"), "x" * 50)
        await eng.sync_wrapper("broken.md")
        eng.log.error.assert_called()

    @pytest.mark.asyncio
    async def test_active_tasks_cleaned_on_exception(self, eng, cfg):
        eng.io.async_copy = AsyncMock(side_effect=RuntimeError("boom"))
        _write(_local(cfg, "task.md"), "x" * 50)
        eng.active_tasks.add("task.md")
        await eng.sync_wrapper("task.md")
        assert "task.md" not in eng.active_tasks

    @pytest.mark.asyncio
    async def test_active_tasks_cleaned_on_success(self, eng, cfg):
        _write(_local(cfg, "ok.md"), "x" * 50)
        eng.active_tasks.add("ok.md")
        await eng.sync_wrapper("ok.md")
        assert "ok.md" not in eng.active_tasks

# ── run() one-shot ──

class TestRun:
    @pytest.mark.asyncio
    async def test_run_oneshot_processes_all_files(self, eng, cfg):
        _write(_local(cfg, "a.md"), "a" * 50)
        _write(_local(cfg, "b.md"), "a" * 50)
        cfg.run_continuously = False
        await eng.run()
        assert os.path.exists(_icloud(cfg, "a.md"))
        assert os.path.exists(_icloud(cfg, "b.md"))

    @pytest.mark.asyncio
    async def test_run_oneshot_saves_state(self, eng, cfg):
        _write(_local(cfg, "x.md"), "yyy")
        cfg.run_continuously = False
        await eng.run()
        assert os.path.exists(cfg.state_file_path)

    @pytest.mark.asyncio
    async def test_run_cleans_up_old_logs(self, eng, cfg):
        cfg.run_continuously = False
        with patch.object(eng.log, "cleanup_old_logs", new_callable=AsyncMock) as cl:
            await eng.run()
            cl.assert_called_once()

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])