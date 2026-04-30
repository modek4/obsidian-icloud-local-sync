import ctypes
import platform
from enum import Enum

FILE_ATTRIBUTE_OFFLINE = 0x00001000  # "O" - only in cloud, not local
FILE_ATTRIBUTE_PINNED = 0x00080000  # "P" - always local (pinned)
FILE_ATTRIBUTE_UNPINNED = 0x00100000  # "U" - not pinned, can be evicted from local storage
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000  # "R" - recall on access, needs to be downloaded before use
FILE_ATTRIBUTE_RECALL_ON_OPEN  = 0x00040000  # FILE_ATTRIBUTE_EA - "RO" - recall on open, needs to be downloaded before opening
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400 # "L" - placeholder (symlink), not a regular file

class ICloudSyncState(Enum):
    """
    All possible states of iCloud file synchronization, determined by Windows file attributes
    """
    CLOUD_ONLY = "cloud_only"
    PENDING = "pending"
    DOWNLOADING = "downloading"
    LOCAL = "local"
    PINNED  = "pinned"
    UNKNOWN = "unknown"

    @property
    def is_safe(self) -> bool:
        """
        Returns:
            bool: True if the file is safe to read/copy (i.e., has local content available)
        """
        return self in (ICloudSyncState.LOCAL, ICloudSyncState.PINNED)

    @property
    def status(self) -> str:
        """
        Simple status for logging

        Returns:
            str: A short status string for logging purposes.
        """
        return {
            ICloudSyncState.CLOUD_ONLY: "iCloud-only",
            ICloudSyncState.DOWNLOADING: "Downloading",
            ICloudSyncState.PENDING: "Pending",
            ICloudSyncState.LOCAL: "Local",
            ICloudSyncState.PINNED: "Pinned",
            ICloudSyncState.UNKNOWN: "Unknown",
        }.get(self, "?")


class ICloudStatusChecker:
    """
    Checks iCloud sync status of files on Windows by reading file attributes, uses Windows API via ctypes
    """
    def __init__(self):
        self._available = platform.system() in ("Windows", "Microsoft")
        if self._available:
            try:
                self._k32 = ctypes.WinDLL('kernel32', use_last_error=True)
                self._k32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
                self._k32.GetFileAttributesW.restype = ctypes.c_uint32
                # PHCM_EXPOSE_PLACEHOLDERS
                try:
                    ntdll = ctypes.WinDLL('ntdll')
                    ntdll.RtlSetProcessPlaceholderCompatibilityMode(2)
                except Exception:
                    pass
            except (AttributeError, OSError):
                self._available = False
                self._k32 = None
        else:
            self._k32 = None

    def detect(self, path: str) -> ICloudSyncState:
        """
        Checks the iCloud sync status of a file on Windows by reading its file attributes.

        Args:
            path: The absolute path to the file (iCloud vault).
        Returns:
            ICloudSyncState corresponding to the current status of the file.
        """
        if not self._available:
            return ICloudSyncState.UNKNOWN

        try:
            attrs = self._k32.GetFileAttributesW(str(path))
        except Exception:
            return ICloudSyncState.UNKNOWN
        if attrs == 0xFFFFFFFF:
            return ICloudSyncState.UNKNOWN

        is_offline = bool(attrs & FILE_ATTRIBUTE_OFFLINE)
        is_pinned = bool(attrs & FILE_ATTRIBUTE_PINNED)
        is_unpinned = bool(attrs & FILE_ATTRIBUTE_UNPINNED)
        is_recall = bool(attrs & (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN))
        is_reparse = bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)

        if is_offline and is_pinned:
            return ICloudSyncState.DOWNLOADING
        elif is_offline and is_recall:
            return ICloudSyncState.PENDING
        # WARNING: no symlinks in vaults allowed, otherwise they will be detected as cloud-only and skipped
        elif is_offline or is_recall or (is_reparse and not is_pinned):
            return ICloudSyncState.CLOUD_ONLY
        elif is_pinned:
            return ICloudSyncState.PINNED
        elif is_unpinned:
            return ICloudSyncState.LOCAL
        else:
            return ICloudSyncState.LOCAL

    def is_safe(self, path: str) -> bool:
        """
        Checks if a file is safe to read/copy (i.e., has local content available).

        Args:
            path: The absolute path to the file.
        Returns:
            bool: True if the file is safe to read/copy, False otherwise.
        """
        state = self.detect(path)
        return state.is_safe or state == ICloudSyncState.UNKNOWN
