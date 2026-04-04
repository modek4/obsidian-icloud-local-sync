import os
import asyncio
import shutil
import ctypes
from ctypes import wintypes
from datetime import datetime

# ── Windows API ──────────────────────────────────────────────────
MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_WRITE_THROUGH = 0x8
FILE_ATTRIBUTE_NORMAL = 0x80

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

MoveFileExW = kernel32.MoveFileExW
MoveFileExW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
MoveFileExW.restype = wintypes.BOOL

SetFileAttributesW = kernel32.SetFileAttributesW
SetFileAttributesW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
SetFileAttributesW.restype = wintypes.BOOL


# ── Utility functions (used across modules) ──────────────────────

def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def safe_exists(path):
    try:
        return os.path.exists(path)
    except Exception:
        return False


def size_or_zero(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0


def safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0


# ── Disk I/O class ───────────────────────────────────────────────

class DiskIO:
    def __init__(self, config, logger):
        self.config = config
        self.log = logger

    def _set_normal_attributes(self, path):
        try:
            if not os.path.exists(path):
                return True
            return bool(SetFileAttributesW(path, FILE_ATTRIBUTE_NORMAL))
        except Exception:
            return False

    async def async_copy(self, src, dst, max_retries=12, initial_backoff=0.25):
        """Copy src -> dst atomically with retries and Windows fallbacks."""
        self.log.info("COPYING", f"{self.config.disp(src)} → {self.config.disp(dst)}", level="verbose")
        ensure_dir(os.path.dirname(dst))
        tmp = dst + ".tmp"

        try:
            await asyncio.to_thread(shutil.copy2, src, tmp)
        except Exception as e:
            self.log.error("FAILED", f"Write tmp file {self.config.disp(tmp)}: {e}")
            raise

        backoff = initial_backoff
        attempt = 0
        while True:
            try:
                if os.path.exists(dst):
                    self._set_normal_attributes(dst)
                os.replace(tmp, dst)
                self.log.success("SUCCESS", f"Updated: {self.config.disp(dst)}", level="verbose")
                return
            except PermissionError:
                attempt += 1
                if attempt >= max_retries:
                    # Win32 MoveFileEx fallback
                    try:
                        ok = MoveFileExW(tmp, dst, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)
                        if ok:
                            self.log.success("SUCCESS", f"MoveFileEx: {self.config.disp(dst)}", level="verbose")
                            return
                        self.log.error("FAILED", f"MoveFileEx (err {ctypes.get_last_error()})")
                    except Exception as exc:
                        self.log.error("DANGER", f"MoveFileEx exception: {exc}")
                    # Final brute-force attempt
                    try:
                        if os.path.exists(dst):
                            os.remove(dst)
                        os.replace(tmp, dst)
                        self.log.success("SUCCESS", f"Forced replace: {self.config.disp(dst)}", level="verbose")
                        return
                    except Exception as exc:
                        self.log.error("FAILED", f"Final forced replace failed: {exc}")
                        try:
                            if os.path.exists(tmp):
                                os.remove(tmp)
                        except Exception:
                            pass
                        raise PermissionError(f"Unable to replace {dst}") from exc

                self._set_normal_attributes(dst)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.8, 5.0)
            except Exception as unexpected:
                self.log.error("DANGER", f"Unexpected error during replace: {unexpected}")
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass
                raise

    async def remove_file(self, path, description):
        """Async remove a file and clean up empty parent dirs."""
        try:
            if not os.path.exists(path):
                return
            await asyncio.to_thread(os.remove, path)
            self.log.success("SUCCESS", f"Removed {description}: {self.config.disp(path)}", level="verbose")
            # Walk up and remove empty directories
            roots = {self.config.local_vault, self.config.icloud_vault, self.config.history_dir}
            dir_path = os.path.dirname(path)
            while dir_path and dir_path not in roots:
                if os.path.exists(dir_path) and not os.listdir(dir_path):
                    try:
                        await asyncio.to_thread(os.rmdir, dir_path)
                        self.log.info("INFO", f"Removed empty dir: {self.config.disp(dir_path)}", level="verbose")
                        dir_path = os.path.dirname(dir_path)
                    except OSError:
                        break
                else:
                    break
        except Exception as e:
            self.log.error("FAILED", f"Remove {description} {path}: {e}")

    def remove_file_sync(self, path, description):
        """Synchronous file removal (used at startup)."""
        try:
            if os.path.exists(path):
                os.remove(path)
                self.log.success("SUCCESS", f"Removed {description}: {self.config.disp(path)}", level="verbose")
        except Exception as e:
            self.log.error("FAILED", f"Remove {description} {path}: {e}")

    async def create_conflict_duplicate(self, path):
        base, ext = os.path.splitext(path)
        conflict = f"{base}_CONFLICT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
        self.log.warn("WARNING", f"Creating conflict duplicate: {self.config.disp(conflict)}", level="verbose")
        try:
            await asyncio.to_thread(shutil.copy2, path, conflict)
        except Exception as e:
            self.log.error("DANGER", f"Failed to create conflict duplicate: {e}")
