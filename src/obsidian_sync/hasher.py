import os
import json
import hashlib
import asyncio

import aiofiles

from .disk_io import safe_exists, safe_mtime, size_or_zero


class FileHasher:
    def __init__(self, config, logger):
        self.config = config
        self.log = logger
        self.state: dict = {}
        self.dirty: bool = False

    def load_state(self):
        path = self.config.state_file_path
        if not os.path.exists(path):
            self.state = {}
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.state = json.load(f)
        except Exception as e:
            self.log.warn("WARNING", f"Loading state file failed: {e}", level="important")
            self.state = {}

    def save_state(self):
        try:
            # Remove empty entries
            empty = [k for k, v in self.state.items() if not v]
            for k in empty:
                del self.state[k]
            with open(self.config.state_file_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            self.dirty = False
        except Exception as e:
            self.log.warn("WARNING", f"Saving state file failed: {e}", level="important")

    async def hash_file(self, path, max_retries=6):
        """Compute SHA-256 of a file with retries for locked files."""
        if not os.path.exists(path):
            return None
        attempt = 0
        backoff = 0.05
        while True:
            h = hashlib.sha256()
            try:
                async with aiofiles.open(path, "rb") as f:
                    while True:
                        chunk = await f.read(8192)
                        if not chunk:
                            break
                        h.update(chunk)
                return h.hexdigest()
            except (PermissionError, OSError) as e:
                attempt += 1
                if attempt > max_retries:
                    self.log.error("FAILED", f"Hashing giving up on {path}: {e}")
                    return None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 1.0)

    async def get_cached_hash(self, path, side, rel_path, force=False):
        """Get hash using mtime/size cache to avoid unnecessary disk reads."""
        if not safe_exists(path):
            # Clean cache for missing file
            if rel_path in self.state and side in self.state[rel_path]:
                del self.state[rel_path][side]
                self.dirty = True
                if not self.state[rel_path]:
                    del self.state[rel_path]
            return None

        mtime = safe_mtime(path)
        size = size_or_zero(path)

        # Check cache
        if not force and rel_path in self.state and side in self.state[rel_path]:
            cached = self.state[rel_path][side]
            if cached['mtime'] == mtime and cached['size'] == size:
                return cached['hash']

        # Cache miss — hash from disk
        h = await self.hash_file(path)
        if h is None:
            return None
        self.state.setdefault(rel_path, {})[side] = {
            'mtime': mtime, 'size': size, 'hash': h
        }
        self.dirty = True
        return h
