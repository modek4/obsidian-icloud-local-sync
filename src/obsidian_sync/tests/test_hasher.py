import os
import json
import hashlib
import asyncio
import pytest
import aiofiles
from unittest.mock import patch, MagicMock, AsyncMock
from conftest import FileHasher

# ── Load State ──

class TestLoadState:
    def test_empty_state_if_no_file(self, hasher):
        hasher.load_state()
        assert hasher.state == {}

    def test_loads_valid_state(self, hasher, cfg):
        data = {"note.md": {"L": {"mtime": 1.0, "size": 10, "hash": "abc"}}}
        path = cfg.state_file_path
        with open(path, "w") as f:
            json.dump(data, f)
        hasher.load_state()
        assert "note.md" in hasher.state

    def test_handles_corrupt_json(self, hasher, cfg):
        path = cfg.state_file_path
        with open(path, "w") as f:
            f.write("{invalid json}")
        hasher.load_state()
        assert hasher.state == {}

    def test_creates_backup_of_corrupt_file(self, hasher, cfg):
        path = cfg.state_file_path
        with open(path, "w") as f:
            f.write("{invalid}")
        hasher.load_state()
        assert os.path.exists(path + ".corrupt")

# ── Save State ──

class TestSaveState:
    def test_saves_state_to_file(self, hasher, cfg):
        hasher.state = {"note.md": {"L": {"mtime": 1.0, "size": 5, "hash": "abc"}}}
        hasher.save_state()
        assert os.path.exists(cfg.state_file_path)
        with open(cfg.state_file_path) as f:
            loaded = json.load(f)
        assert "note.md" in loaded

    def test_removes_empty_entries(self, hasher, cfg):
        hasher.state = {"note.md": {}, "other.md": {"L": {"hash": "x"}}}
        hasher.save_state()
        with open(cfg.state_file_path) as f:
            loaded = json.load(f)
        assert "note.md" not in loaded

    def test_clears_dirty_flag(self, hasher, cfg):
        hasher.state = {"a.md": {"L": {"mtime": 1.0, "size": 1, "hash": "x"}}}
        hasher.dirty = True
        hasher.save_state()
        assert hasher.dirty is False

    def test_atomic_write_via_tmp(self, hasher, cfg):
        hasher.state = {"f.md": {"L": {"mtime": 1.0, "size": 1, "hash": "x"}}}
        with patch("os.replace") as mock_replace:
            hasher.save_state()
            mock_replace.assert_called_once()
            args = mock_replace.call_args[0]
            assert args[0].endswith(".tmp")

# ── Hash File ──

class TestHashFile:
    @pytest.mark.asyncio
    async def test_hashes_file_content(self, hasher, tmp_path):
        f = tmp_path / "note.md"
        f.write_bytes(b"hello world")
        result = await hasher.hash_file(str(f))
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert result == expected

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_file(self, hasher, tmp_path):
        result = await hasher.hash_file(str(tmp_path / "ghost.md"))
        assert result is None

    @pytest.mark.asyncio
    async def test_retries_on_permission_error(self, hasher, tmp_path):
        f = tmp_path / "locked.md"; f.write_text("x")
        call_count = [0]
        real_open = aiofiles.open
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock(read=AsyncMock(side_effect=lambda size=-1: b"x")))
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        def flaky_open(path, mode="rb", **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise PermissionError("locked")
            return real_open(path, mode, **kwargs)
        with patch("aiofiles.open", side_effect=flaky_open):
            result = await hasher.hash_file(str(f))
        assert call_count[0] == 3

    @pytest.mark.asyncio
    async def test_returns_none_after_max_retries(self, hasher, tmp_path):
        f = tmp_path / "locked.md"; f.write_text("x")
        with patch("aiofiles.open", side_effect=PermissionError("always locked")):
            result = await hasher.hash_file(str(f))
        assert result is None

# ── Get Cached Hash ──

class TestGetCachedHash:
    def _put_cache(self, hasher, rel, side, mtime, size, hash_val):
        hasher.state.setdefault(rel, {})[side] = {
            "mtime": mtime, "size": size, "hash": hash_val
        }

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_hash(self, hasher, tmp_path, cfg):
        f = tmp_path / "note.md"; f.write_bytes(b"content")
        mtime = os.path.getmtime(str(f))
        size  = os.path.getsize(str(f))
        self._put_cache(hasher, "note.md", "L", mtime, size, "cached_hash")
        result = await hasher.get_cached_hash(str(f), "L", "note.md")
        assert result == "cached_hash"

    @pytest.mark.asyncio
    async def test_cache_miss_rehashes(self, hasher, tmp_path):
        f = tmp_path / "note.md"; f.write_bytes(b"hello")
        expected = hashlib.sha256(b"hello").hexdigest()
        self._put_cache(hasher, "note.md", "L", 0.0, 0, "old_hash")
        result = await hasher.get_cached_hash(str(f), "L", "note.md")
        assert result == expected

    @pytest.mark.asyncio
    async def test_force_bypasses_cache(self, hasher, tmp_path):
        f = tmp_path / "note.md"; f.write_bytes(b"hello")
        mtime = os.path.getmtime(str(f))
        size  = os.path.getsize(str(f))
        self._put_cache(hasher, "note.md", "L", mtime, size, "cached_hash")
        expected = hashlib.sha256(b"hello").hexdigest()
        result = await hasher.get_cached_hash(str(f), "L", "note.md", force=True)
        assert result == expected

    @pytest.mark.asyncio
    async def test_missing_file_returns_none(self, hasher, tmp_path):
        result = await hasher.get_cached_hash(str(tmp_path / "ghost.md"), "L", "ghost.md")
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_file_cleans_cache(self, hasher, tmp_path):
        self._put_cache(hasher, "gone.md", "L", 1.0, 10, "old")
        await hasher.get_cached_hash(str(tmp_path / "gone.md"), "L", "gone.md")
        assert "L" not in hasher.state.get("gone.md", {})

    @pytest.mark.asyncio
    async def test_updates_cache_after_rehash(self, hasher, tmp_path):
        f = tmp_path / "note.md"; f.write_bytes(b"data")
        await hasher.get_cached_hash(str(f), "L", "note.md")
        assert "note.md" in hasher.state
        assert "L" in hasher.state["note.md"]
        assert "hash" in hasher.state["note.md"]["L"]

    @pytest.mark.asyncio
    async def test_rehash_marks_dirty(self, hasher, tmp_path):
        f = tmp_path / "note.md"; f.write_bytes(b"data")
        hasher.dirty = False
        await hasher.get_cached_hash(str(f), "L", "note.md")
        assert hasher.dirty is True