import sys, os, types, ctypes, importlib.util
import pytest
import warnings

from pathlib import Path
from unittest.mock import MagicMock, patch

SOURCE_FILES_DIR = Path(__file__).parent.parent
MODULE_FILES = {
    "config": "config.py",
    "logger": "logger.py",
    "disk_io": "disk_io.py",
    "hasher": "hasher.py",
    "duplicates": "duplicates.py",
    "sync_engine": "sync_engine.py",
    "icloud_status": "icloud_status.py",
}
PKG = "obsidian_sync"

mock_k32 = MagicMock(name="kernel32")
mock_windll = MagicMock(name="windll")
mock_windll.kernel32 = mock_k32

_wt = types.ModuleType("ctypes.wintypes")
_wt.LPCWSTR = ctypes.c_wchar_p
_wt.DWORD = ctypes.c_uint32
_wt.BOOL = ctypes.c_int
sys.modules.setdefault("ctypes.wintypes", _wt)
if not hasattr(ctypes, "WinDLL"):
    ctypes.WinDLL = MagicMock(return_value=mock_k32)

def _load(sub):
    fname = MODULE_FILES.get(sub, sub + ".py")
    path  = SOURCE_FILES_DIR / fname
    if not path.exists():
        stub = types.ModuleType(f"{PKG}.{sub}")
        sys.modules[f"{PKG}.{sub}"] = stub
        return stub
    spec = importlib.util.spec_from_file_location(f"{PKG}.{sub}", path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = PKG
    sys.modules[f"{PKG}.{sub}"] = mod
    with patch("platform.system", return_value="Windows", create=True), \
         patch("ctypes.windll", mock_windll, create=True), \
         patch("ctypes.WinDLL", MagicMock(return_value=mock_k32), create=True):
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            warnings.warn(f"[conftest] Failed to load '{sub}': {e}", stacklevel=2)
            stub = types.ModuleType(f"{PKG}.{sub}")
            stub._load_error = str(e)
            sys.modules[f"{PKG}.{sub}"] = stub
            return stub
    return mod

_mods = {s: _load(s) for s in ("config","logger","disk_io","hasher","duplicates","icloud_status","sync_engine")}

def _get(mn, attr):
    m = _mods.get(mn)
    if m and hasattr(m, attr):
        return getattr(m, attr)
    warnings.warn(f"[conftest] '{mn}.{attr}' not found, using MagicMock", stacklevel=2)
    return MagicMock()

SyncConfig = _get("config", "SyncConfig")
SyncLogger = _get("logger", "SyncLogger")
strip_ansi = _get("logger", "strip_ansi")
colored = _get("logger", "colored")
LEVEL_MAP = _get("logger", "LEVEL_MAP")
DiskIO = _get("disk_io", "DiskIO")
safe_exists = _get("disk_io", "safe_exists")
size_or_zero = _get("disk_io", "size_or_zero")
safe_mtime = _get("disk_io", "safe_mtime")
ensure_dir = _get("disk_io", "ensure_dir")
FileHasher = _get("hasher", "FileHasher")
DuplicateScanner = _get("duplicates", "DuplicateScanner")
SyncEngine = _get("sync_engine", "SyncEngine")
ICloudStatusChecker = _get("icloud_status","ICloudStatusChecker")
ICloudSyncState = _get("icloud_status","ICloudSyncState")

try:
    _icm = _mods["icloud_status"]
    FILE_ATTRIBUTE_OFFLINE = _icm.FILE_ATTRIBUTE_OFFLINE
    FILE_ATTRIBUTE_PINNED = _icm.FILE_ATTRIBUTE_PINNED
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = _icm.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    FILE_ATTRIBUTE_REPARSE_POINT = _icm.FILE_ATTRIBUTE_REPARSE_POINT
except AttributeError as e:
    import warnings
    warnings.warn(f"[conftest] icloud_status constants not found: {e}")
    FILE_ATTRIBUTE_OFFLINE = 0x00001000
    FILE_ATTRIBUTE_PINNED = 0x00080000
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

@pytest.fixture
def cfg(tmp_path):
    for d in ("local", "icloud", "history", "logs"):
        (tmp_path / d).mkdir()
    c = SyncConfig()
    c.local_vault = str(tmp_path / "local")
    c.icloud_vault = str(tmp_path / "icloud")
    c.history_dir = str(tmp_path / "history")
    c.logs_dir = str(tmp_path / "logs")
    c.run_continuously = False
    c.user_interface = False
    c.check_icloud_status = True
    c.poll_interval = 2
    c.stability_window = 0
    c.stabilize_wait = 0
    c.cooldown_seconds = 0
    c.big_file_cooldown = 0
    c.big_file_threshold = 100 * 1024
    c.tiny_threshold = 8
    c.max_concurrent_io = 50
    c.console_level = "quiet"
    c.check_icloud_status = False
    c.ignore_patterns = []
    c.ignored_dirs = set()
    c.ignored_files = set()
    c.shorter_paths = True
    c.max_display_length = 60
    c.log_retention = 5
    c.conflict_resolution = "newer"
    return c

@pytest.fixture
def mock_log(): return MagicMock(spec=SyncLogger)

@pytest.fixture
def mock_disk_io(): return MagicMock()

@pytest.fixture
def hasher(cfg, mock_log):
    h = FileHasher(cfg, mock_log); h.state = {}; h.dirty = False; return h

@pytest.fixture
def eng(cfg, mock_log):
    with patch("platform.system", return_value="Windows", create=True):
        io = DiskIO(cfg, mock_log)
    h = FileHasher(cfg, mock_log); h.state = {}
    return SyncEngine(cfg, mock_log, h, io, MagicMock())