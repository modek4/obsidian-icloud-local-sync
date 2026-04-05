import os
import json
import hashlib
import asyncio

import aiofiles
import shutil

from typing import Literal
from .disk_io import safe_exists, safe_mtime, size_or_zero

class FileHasher:
    """
    Computes and caches file hashes to optimize synchronization performance. Maintains a JSON-based state file that caches the SHA-256 hash of files alongside their last mtime and size. By checking the cache first, the sync engine avoids expensive disk reads for files that haven't changed.
    """
    def __init__(self, config, logger):
        """
        Initializes the FileHasher.

        Args:
            config (SyncConfig): Configuration instance containing the state file path.
            logger (SyncLogger): Logger instance for warnings and errors.
        """
        self.config = config
        self.log = logger
        self.state: dict = {}
        self.dirty: bool = False

    def load_state(self):
        """
        Loads the cached file states from the JSON state file. If the file doesn't exist, it initializes an empty state. If the file is corrupt or unreadable, it creates a backup of the corrupted file and resets the state to an empty dictionary to prevent sync failure.
        """
        path = self.config.state_file_path
        if not os.path.exists(path):
            self.state = {}
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.state = json.load(f)
        except Exception as e:
            self.log.warn("WARNING", f"Loading state file failed: {e}", level="important")
            try:
                shutil.copy2(path, path + ".corrupt")
                self.log.warn("WARNING", f"Corrupt state backed up to: {path}.corrupt", level="important")
            except Exception:
                pass
            self.state = {}

    def save_state(self):
        """
        Cleans up any empty file entries before saving. It writes data to a temporary file first, then atomically replaces the target state file to prevent corruption in case of unexpected termination (e.g., power loss).
        """
        tmp_path = self.config.state_file_path + ".tmp"
        try:
            # Remove empty entries
            empty = [k for k, v in self.state.items() if not v]
            for k in empty:
                del self.state[k]
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            os.replace(tmp_path, self.config.state_file_path)
            self.dirty = False
        except Exception as e:
            self.log.warn("WARNING", f"Saving state file failed: {e}", level="important")
            try:
                os.remove(tmp_path)
            except Exception:
                pass

    async def hash_file(self, path, max_retries=6):
        """
        Reads the file in chunks to keep memory usage low. Incorporates a retry mechanism with exponential backoff to handle transient file locks (e.g., background iCloud sync activity).

        Args:
            path (str): The absolute file path to hash.
            max_retries (int, optional): Number of attempts before giving up.
        Returns:
            Optional[str]: The computed SHA-256 hex digest, or None if the file doesn't exist or couldn't be read after all retries.
        """
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
                if attempt >= max_retries:
                    self.log.error("FAILED", f"Hashing giving up on {path}: {e}", level="important")
                    return None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 1.0)

    async def get_cached_hash(self, path: str, side: Literal['L', 'C', 'H'], rel_path: str, force: bool = False):
        """
        Retrieves the SHA-256 file hash, utilizing the state cache when possible. Compares the current `mtime` and file size with the cached values. If they match, the cached hash is returned instantly. Otherwise, the file is re-hashed and the cache updated.

        Args:
            path (str): The absolute file path to examine.
            side (Literal['L', 'C', 'H']): Indicates which vault the file belongs to (Local, iCloud, History).
            rel_path (str): The relative path used as the cache dictionary key.
            force (bool, optional): If True, bypasses the cache check and hashes the file from disk regardless of mtime/size. Defaults to False.
        Returns:
            Optional[str]: The file hash string, or None if the file is missing or unreadable.
        """
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
