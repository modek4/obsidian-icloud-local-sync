import os
import pytest
from unittest.mock import patch, MagicMock

from conftest import (FILE_ATTRIBUTE_PINNED, FILE_ATTRIBUTE_OFFLINE, FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,ICloudStatusChecker, ICloudSyncState)

# ── Fixtures ──

@pytest.fixture
def checker():
    with patch("platform.system", return_value="Windows"):
        c = ICloudStatusChecker()
    mock_k32 = MagicMock()
    mock_k32.GetFileAttributesW.return_value = 0x00000020
    mock_k32.GetFileAttributesW.argtypes = None
    mock_k32.GetFileAttributesW.restype = None
    c._k32 = mock_k32
    c._available = True
    return c

# ── ICloudSyncState ──

class TestSyncStateIsSafe:
    def test_local_is_safe(self):
        assert ICloudSyncState.LOCAL.is_safe is True

    def test_pinned_is_safe(self):
        assert ICloudSyncState.PINNED.is_safe is True

    def test_cloud_only_not_safe(self):
        assert ICloudSyncState.CLOUD_ONLY.is_safe is False

    def test_downloading_not_safe(self):
        assert ICloudSyncState.DOWNLOADING.is_safe is False

    def test_unknown_enum_not_safe(self):
        assert ICloudSyncState.UNKNOWN.is_safe is False

    def test_all_states_have_status(self):
        for state in ICloudSyncState:
            assert hasattr(state, "status"), f"{state} missing status attribute"
            assert isinstance(state.status, str)
            assert len(state.status) > 0

# ── ICloudStatusChecker ──

class TestDetect:
    def test_local_no_cloud_flags(self, checker):
        checker._k32.GetFileAttributesW.return_value = 0x00000020
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.LOCAL

    def test_pinned(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_PINNED
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.PINNED

    def test_cloud_only_via_offline(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_OFFLINE
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.CLOUD_ONLY

    def test_cloud_only_via_recall(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.CLOUD_ONLY

    def test_downloading_offline_plus_pinned(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_PINNED
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.DOWNLOADING

    def test_invalid_file_attributes_returns_unknown(self, checker):
        checker._k32.GetFileAttributesW.return_value = 0xFFFFFFFF
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.UNKNOWN

    def test_winapi_exception_returns_unknown(self, checker):
        checker._k32.GetFileAttributesW.side_effect = OSError("WinAPI crash")
        assert checker.detect("C:/fake/file.md") == ICloudSyncState.UNKNOWN

    def test_unavailable_checker_returns_unknown(self):
        with patch("platform.system", return_value="Linux"):
            c = ICloudStatusChecker()
            assert c.detect("C:/fake/file.md") == ICloudSyncState.UNKNOWN

# ── Is Safe ──

class TestIsSafe:
    def test_local_is_safe(self, checker):
        checker._k32.GetFileAttributesW.return_value = 0x00000020
        assert checker.is_safe("C:/fake/file.md") is True

    def test_pinned_is_safe(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_PINNED
        assert checker.is_safe("C:/fake/file.md") is True

    def test_cloud_only_is_not_safe(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_OFFLINE
        assert checker.is_safe("C:/fake/file.md") is False

    def test_downloading_is_not_safe(self, checker):
        checker._k32.GetFileAttributesW.return_value = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_PINNED
        assert checker.is_safe("C:/fake/file.md") is False

    def test_unknown_is_safe_fail_open(self, checker):
        checker._k32.GetFileAttributesW.return_value = 0xFFFFFFFF
        assert checker.is_safe("C:/fake/file.md") is True

    def test_unavailable_platform_is_safe(self):
        with patch("platform.system", return_value="Darwin"):
            c = ICloudStatusChecker()
            assert c.is_safe("C:/fake/file.md") is True

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])