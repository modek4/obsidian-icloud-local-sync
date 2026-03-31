from colorama import Fore, Style, init as colorama_init
import asyncio
import aiofiles
import hashlib
import json
import os
import shutil
from datetime import datetime
import ctypes
from ctypes import wintypes
import time
import traceback
import re
import sys
import fnmatch

# ----------------- CONFIG START -----------------
# Windows local path
LOCAL_VAULT = r"C:\Obsidian\Vault"
# Windows iCloud path
ICLOUD_VAULT = r"C:\Users\user\iCloudDrive\iCloud~md~obsidian"
# History changelog path
HISTORY_DIR = r"C:\Obsidian\History"
# Log files directory
LOGS_DIR = r"C:\Obsidian\Logs"
# Patterns to ignore during sync (relative paths, filenames, or folder names)
IGNORE_PATTERNS = [
# Uncomment and customize these patterns
    # ".obsidian/plugins",
    # ".obsidian/themes",
    # "*.canvas",
    # "Templates/*",
    # "Private/Note.md",
    # ".obsidian/plugins/recent-files-obsidian",
]
# Common system and sync-related directories that are not relevant for syncing
IGNORED_DIRS = {
    '.trash',
    '.fseventsd',
    '.spotlight-v100',
    '.apdisk'
}
# Temporary and system files that are not relevant for syncing
IGNORED_FILES = {
    '.ds_store',
    '.trash',
    'workspace.json'
}
# Run mode: one-shot vs continuous daemon
RUN_CONTINUOUSLY = True
# Console verbosity level: "quiet" (summary only), "normal" (key events), "verbose" (everything)
LOG_CONSOLE_LEVEL = "normal"
# Use shorter relative paths in logs and state file for memory and readability
SHORTER_PATHS = True
# Maximum length of displayed paths in logs (for readability)
MAX_DISP_LEN = 50
# State file for caching hashes and mtimes to minimize disk I/O
STATE_FILE = "sync_state.json"
# Cooldown settings
COOLDOWN_SECONDS = 3
# Longer cooldown for big files to avoid thrashing during edits (e.g. large images or PDFs)
BIG_FILE_COOLDOWN = 30
# Polling interval for the main loop (in seconds)
POLL_INTERVAL = 2
# Stability window for create/delete operations to avoid reacting to transient states (in seconds)
STABILITY_WINDOW = 3
# Longer stabilize wait for Case D where both sides changed, to give user time to finish editing and avoid false conflict resolution
STABILIZE_WAIT = 8
# Tiny file threshold to ignore transient creates/deletes for very small files
TINY_THRESHOLD = 8
# Big file threshold to apply longer cooldowns and more cautious handling to avoid sync thrashing during edits
BIG_FILE_THRESHOLD = 100 * 1024
# Current log file (initialized in main)
CURRENT_LOG_FILE = None
# Number of recent logs to keep, old ones will be deleted on startup to prevent disk clutter
LOG_RETENTION = 10
# ----------------- CONFIG END -------------------

# initialize colorama for Windows console
colorama_init()
# Windows constants for MoveFileEx
MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_WRITE_THROUGH = 0x8
# SetFileAttributes flags
FILE_ATTRIBUTE_NORMAL = 0x80
# ctypes wrappers
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
MoveFileExW = kernel32.MoveFileExW
MoveFileExW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
MoveFileExW.restype = wintypes.BOOL
SetFileAttributesW = kernel32.SetFileAttributesW
SetFileAttributesW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
SetFileAttributesW.restype = wintypes.BOOL
# rel_path -> timestamp until which file is on cooldown
cooldowns = {}
# tracks currently processing rel_paths to avoid parallel overlapping
active_tasks = set()
# flag to know when to save the state file
state_dirty = False
# limit concurrent IO operations to avoid overwhelming the disk
io_semaphore = asyncio.Semaphore(50)
# console output levels for filtering logs
LEVEL_MAP = {
    "quiet": 0,
    "normal": 10,
    "verbose": 100,
    "important": 1000
}
# mapping of icons to ASCII for log files
ICON_TO_ASCII = {"🔵": "[i]", "🟡": "[w]", "🔴": "[!]", "🟢": "[+]", "⚪": "[s]", "⚫": "[.]"}
# Save STATE_FILE in LOGS_DIR
STATE_FILE_PATH = os.path.join(LOGS_DIR, STATE_FILE)

# ---------- Path helpers ----------
# Startup config validation to ensure all paths exist and are properly set up before starting the sync process.
def validate_config():
    for name, path in [("LOCAL_VAULT", LOCAL_VAULT), ("ICLOUD_VAULT", ICLOUD_VAULT), ("HISTORY_DIR", HISTORY_DIR), ("LOGS_DIR", LOGS_DIR)]:
        if not os.path.exists(path):
            if name in ["HISTORY_DIR", "LOGS_DIR"]:
                ensure_dir(path)
                console_event("🟡", Fore.YELLOW, "CONFIG", f"{name} did not exist, created: {path}", level="important")
                continue
            console_event("🔴", Fore.RED, "CONFIG", f"{name} does not exist: {path}", level="important")
            sys.exit(1)
    if os.path.normcase(LOCAL_VAULT) == os.path.normcase(ICLOUD_VAULT):
        console_event("🔴", Fore.RED, "CONFIG", "LOCAL_VAULT and ICLOUD_VAULT cannot be the same path!", level="important")
        sys.exit(1)
    for name, guarded in [("LOCAL_VAULT", LOCAL_VAULT), ("ICLOUD_VAULT", ICLOUD_VAULT)]:
        if os.path.normcase(HISTORY_DIR).startswith(os.path.normcase(guarded) + os.sep):
            console_event("🔴", Fore.RED, "CONFIG", f"HISTORY_DIR cannot be inside {name}!", level="important")
            sys.exit(1)
    for p in IGNORE_PATTERNS:
        if '//' in p or p.startswith('/'):
            console_event("🟡", Fore.YELLOW, "CONFIG", f"Suspicious IGNORE_PATTERN: '{p}'", level="important")

# Determines if a given relative path should be ignored based on IGNORE_PATTERNS
def is_ignored(rel_path: str) -> bool:
    # Returns True if the given relative path matches any IGNORE_PATTERNS
    rel_normalized = rel_path.replace(os.sep, '/').lower()
    for pattern in IGNORE_PATTERNS:
        pattern_normalized = pattern.replace(os.sep, '/').lower()
        # 1. Exact match
        if rel_normalized == pattern_normalized:
            return True
        # 2. Glob match against full path (e.g. "*.canvas", "Templates/*")
        if fnmatch.fnmatch(rel_normalized, pattern_normalized):
            return True
        # 3. Folder prefix match (e.g. ".obsidian/plugins" blocks all files inside it)
        if rel_normalized.startswith(pattern_normalized.rstrip('/') + '/'):
            return True
    return False

# Provides a shortened display version of the path for logs, improving readability while keeping key info.
def disp(path):
    if not SHORTER_PATHS:
        return path
    # If the path is absolute and starts with one of our known roots, convert it to a relative path for cleaner display
    if os.path.isabs(path):
        if path.startswith(LOCAL_VAULT):
            path = os.path.relpath(path, LOCAL_VAULT)
        elif path.startswith(ICLOUD_VAULT):
            path = os.path.relpath(path, ICLOUD_VAULT)
        elif path.startswith(HISTORY_DIR):
            path = os.path.relpath(path, HISTORY_DIR)
        else:
            # If it's an absolute path but doesn't match known roots, just show the filename to avoid clutter
            return os.path.basename(path)
    # Properly shorten the path (only if it's really long)
    if len(path) <= MAX_DISP_LEN:
        return path
    parts = path.split(os.sep)
    if len(parts) > 2:
        # Show first and last part of the path, with "..." in the middle to indicate truncation
        return f"{parts[0]}{os.sep}...{os.sep}{parts[-1]}"
    # For files in the root directory with very long names
    return f"...{os.sep}{os.path.basename(path)}"

# ---------- Logging helpers ----------
# Regex to match ANSI escape codes
_ANSI_ = re.compile(r'\x1b\[([0-9]{1,3}(;[0-9]{1,2})?)?[mGK]')
def strip_ansi(text):
    # Delete colors for clean log files, using Coloramas output
    return _ANSI_.sub('', text)

# Helper to colorize text for console output using Colorama
def colored(text, color):
    return color + text + Style.RESET_ALL

# Log buffer
_log_buffer: list[str] = []
def _flush_log_buffer():
    if not _log_buffer or not CURRENT_LOG_FILE:
        return
    try:
        with open(CURRENT_LOG_FILE, 'a', encoding='utf-8') as f:
            f.writelines(_log_buffer)
    except Exception as e:
        console_event("🔴", Fore.RED, "FAILED", f"Log Write Failed: {e}", level="important")
    _log_buffer.clear()

# Log to file
def write_to_log_file(msg_type, icon, msg):
    # Appends a log entry to the current log file with timestamp and message type. Strips ANSI color codes for clean logs.
    if not CURRENT_LOG_FILE:
        return
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    clean_msg = strip_ansi(msg)
    # ascii_icon = ICON_TO_ASCII.get(icon, icon) # Optionally
    log_line = f"[{timestamp}] [{msg_type}] {clean_msg}\n"
    _log_buffer.append(log_line)
    if len(_log_buffer) >= 20:
        _flush_log_buffer()

# Log info entry
def log_info(msg_type, msg, level="verbose"):
    console_event("🔵", Fore.CYAN, msg_type, msg, level=level)
    write_to_log_file(msg_type, "🔵", msg)

# Log warning entry
def log_warn(msg_type, msg, level="verbose"):
    console_event("🟡", Fore.YELLOW, msg_type, msg, level=level)
    write_to_log_file(msg_type, "🟡", msg)

# Log error entry
def log_error(msg_type, msg, level="important", critical=False):
    console_event("🔴", Fore.RED, msg_type, msg, level=level)
    write_to_log_file(msg_type, "🔴", msg)
    # Emergency exit
    if critical:
        if RUN_CONTINUOUSLY:
            console_event("🔴", Fore.RED, "ERROR", "An error occurred. Stopping sync to prevent potential data loss. Please check the logs and fix the issue before restarting.", level="important")
            sys.exit(1)
        else:
            console_event("🔴", Fore.RED, "ERROR", "An error occurred during one-shot sync. Please check the logs and fix the issue.", level="important")

# Log success entry
def log_success(msg_type, msg, level="important"):
    console_event("🟢", Fore.GREEN, msg_type, msg, level=level)
    write_to_log_file(msg_type, "🟢", msg)

# Custom log entry
def log_custom(icons, color, msg_type, msg, rel_path, level="normal"):
    if LOG_CONSOLE_LEVEL.lower() != "verbose":
        console_event(icons[0], color[0], msg_type, rel_path, level="normal")
    else:
        console_event(icons[1], color[1], msg_type, msg, level=level)
    write_to_log_file(msg_type, icons[1], msg)

# Console output for key events.
def console_event(icon, color, msg_type, msg, level="normal"):
    level_lower = level.lower()
    ts = datetime.now().strftime('%H:%M:%S')
    message = f"  {ts:<4}" + color + f" {icon} {msg_type:<12}" + Style.RESET_ALL + f" {msg}"
    if level_lower == "important":
        print(message)
        return
    if LOG_CONSOLE_LEVEL.lower() == "quiet":
        return
    current_level = LEVEL_MAP.get(LOG_CONSOLE_LEVEL.lower(), 10)
    event_level = LEVEL_MAP.get(level_lower, 10)
    if current_level < event_level:
        return
    print(message)

# Header for each scan cycle, showing timestamp and number of files checked.
def console_header(files_checked: int):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"  {ts:<4}" + Fore.CYAN + f" 🔵 {'INFO':<12} Scanning... {files_checked} files" + Style.RESET_ALL, flush=True)

# Status message when no changes are detected.
def console_idle():
    if LOG_CONSOLE_LEVEL.lower() == "quiet":
        return
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"  {ts:<4}" + Fore.CYAN + f" 🔵 {'INFO':<12} No changes." + Style.RESET_ALL, end="\r", flush=True)

# Startup banner
def console_startup(mode_str: str):
    print(Fore.WHITE + Style.BRIGHT + "=" * 75 + Style.RESET_ALL)
    print(Fore.CYAN  + Style.BRIGHT + "  Obsidian Sync" + Style.RESET_ALL)
    print(Fore.WHITE + f"  Mode:   {mode_str}" + Style.RESET_ALL)
    print(Fore.WHITE + f"  Local:  {LOCAL_VAULT}" + Style.RESET_ALL)
    print(Fore.WHITE + f"  iCloud: {ICLOUD_VAULT}" + Style.RESET_ALL)
    print(Fore.WHITE + f"  Log:    {CURRENT_LOG_FILE}" + Style.RESET_ALL)
    print(Fore.WHITE + Style.BRIGHT + "=" * 75 + Style.RESET_ALL)

# Clean up old log files, keeping only the most recent 10 to prevent disk clutter over time.
async def cleanup_old_logs(keep=10):
    try:
        if not os.path.exists(LOGS_DIR):
            return
        logs = await asyncio.to_thread(
            lambda: [os.path.join(LOGS_DIR, f) for f in os.listdir(LOGS_DIR) if f.endswith('.log')]
        )
        if len(logs) <= keep:
            return
        # Sort logs by modification time, oldest first
        logs.sort(key=os.path.getmtime)
        # Delete all but the most recent 'keep' logs
        for old_log in logs[:-keep]:
            await asyncio.to_thread(os.remove, old_log)
    except Exception as e:
        console_event("🔴", Fore.RED, "FAILED", f"Log Cleanup Failed: {e}", level="important")

# ----------- Utility helpers ------------
# Ensure a directory exists, creating it if necessary
def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)

# Safe wrappers around os.path functions to handle potential exceptions
def safe_exists(path):
    try:
        return os.path.exists(path)
    except Exception:
        log_error("FAILED", f"Error checking existence of {disp(path)}", level="important")
        return False

# Obsidian config files (.obsidian/) can be legitimately tiny (e.g. empty JSON)
def min_seed_size(rel_path: str) -> int:
    return 1 if '.obsidian' in rel_path.lower() else TINY_THRESHOLD

# Wrappers to avoid exceptions when files are locked or deleted during checks
def size_or_zero(path):
    try:
        return os.path.getsize(path)
    except Exception:
        log_warn("WARNING", f"Treating as 0. Error getting size of {disp(path)}", level="important")
        return 0

# Better mtime
def safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        log_warn("WARNING", f"Treating as 0. Error getting mtime of {disp(path)}", level="important")
        return 0

# State management for caching file hashes and mtimes to minimize disk I/O.
def load_state():
    if not os.path.exists(STATE_FILE_PATH):
        return {}
    try:
        with open(STATE_FILE_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        log_warn("WARNING", f"Loading state file {STATE_FILE} failed: {e}", level="important")
        return {}

# Cleans up state by removing entries for files that no longer exist
def save_state(state):
    try:
        with open(STATE_FILE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log_warn("WARNING", f"Saving state file {STATE_FILE} failed: {e}", level="important")

# ----------- Hashing (async & cached) ------------
# Asynchronously computes SHA-256 hash of a file with retries to handle transient access issues.
async def hash_file(path, max_retries=6):
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
                log_error("FAILED", f"Hashing giving up on {path}: {e}", level="important")
                return None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 1.0)

# Gets hash using cache based on mtime and filesize to avoid disk I/O.
async def get_cached_hash(path, side, rel_path, state, force=False):
    global state_dirty
    if not safe_exists(path):
        # Cleanup cache for missing file
        if rel_path in state and side in state[rel_path]:
            del state[rel_path][side]
            state_dirty = True
            if not state[rel_path]:
                del state[rel_path]
        return None
    mtime = safe_mtime(path)
    size = size_or_zero(path)
    # Check cache unless forced to recalculate
    if not force and rel_path in state and side in state[rel_path]:
        cached = state[rel_path][side]
        if cached['mtime'] == mtime and cached['size'] == size:
            return cached['hash']
    # Cache miss or forced refresh
    h = await hash_file(path)
    if h is None:
        return None
    state.setdefault(rel_path, {})[side] = {'mtime': mtime, 'size': size, 'hash': h}
    state_dirty = True
    return h

# ----------- IO helpers (atomic copy with retries) ------------
# On Windows, files can be locked by other processes (e.g. Obsidian) causing PermissionErrors during copy or replace.
def set_normal_attributes(path):
    try:
        if not os.path.exists(path):
            return True
        res = SetFileAttributesW(path, FILE_ATTRIBUTE_NORMAL)
        return bool(res)
    except Exception:
        return False

# Asynchronously copies a file with retries and Windows-specific fallbacks to handle locked files.
async def async_copy(src, dst, max_retries=12, initial_backoff=0.25):
    # Copy SRC to DST atomically with retries and Windows fallbacks.
    log_info("COPYING", f"{disp(src)} → {disp(dst)}", level="verbose")
    ensure_dir(os.path.dirname(dst))
    tmp = dst + ".tmp"
    try:
        # Run blocking copy in thread so loop remains responsive
        await asyncio.to_thread(shutil.copy2, src, tmp)
    except Exception as e:
        log_error("FAILED", f"Write tmp file {tmp}: {e}", level="important")
        raise
    backoff = initial_backoff
    attempt = 0
    while True:
        try:
            if os.path.exists(dst):
                set_normal_attributes(dst)
            # First try os.replace (atomic same-drive)
            os.replace(tmp, dst)
            log_success("SUCCESS", f"Updated: {dst}", level="verbose")
            return
        except PermissionError as e:
            attempt += 1
            if attempt >= max_retries:
                log_warn("WARNING", "Max retries reached - trying Win32 MoveFileEx fallback", level="verbose")
                try:
                    ok = MoveFileExW(tmp, dst, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)
                    if ok:
                        log_success("SUCCESS", f"MoveFileEx: {dst}", level="verbose")
                        return
                    else:
                        log_error("FAILED", f"MoveFileEx (err {ctypes.get_last_error()}).", level="important")
                except Exception as exc:
                    log_error("DANGER", f"MoveFileEx exception: {exc}", level="important")
                # Final brute-force attempt
                try:
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.replace(tmp, dst)
                    log_success("SUCCESS", f"Forced replace succeeded after removing destination: {dst}", level="verbose")
                    return
                except Exception as exc:
                    log_error("FAILED", f"Final forced replace failed: {exc}", level="important")
                    try:
                        if os.path.exists(tmp): os.remove(tmp)
                    except Exception: pass
                    raise PermissionError(f"Unable to replace {dst}") from exc
            set_normal_attributes(dst)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.8, 5.0)
        except Exception as unexpected:
            log_error("DANGER", f"Unexpected error during replace: {unexpected}", level="important")
            try:
                if os.path.exists(tmp): os.remove(tmp)
            except Exception: pass
            raise

# Creates a conflict duplicate of the given file by copying it with a timestamped name.
async def create_conflict_duplicate(path):
    base, ext = os.path.splitext(path)
    conflict = f"{base}_CONFLICT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
    log_warn("WARNING", f"Creating conflict duplicate: {conflict}", level="verbose")
    try:
        await asyncio.to_thread(shutil.copy2, path, conflict)
    except Exception as e:
        log_error("DANGER", f"Failed to create conflict duplicate {conflict}: {e}", level="important")

# ---------- Core file operations ----------
# Push local file to iCloud and update history
async def push_local_to_icloud(rel):
    local = os.path.join(LOCAL_VAULT, rel)
    icloud = os.path.join(ICLOUD_VAULT, rel)
    history = os.path.join(HISTORY_DIR, rel)
    await async_copy(local, icloud)
    await async_copy(local, history)
    file_size = size_or_zero(local)
    cooldown_time = BIG_FILE_COOLDOWN if file_size > BIG_FILE_THRESHOLD else COOLDOWN_SECONDS
    cooldowns[rel] = time.time() + cooldown_time

# Restore local file from iCloud and update history
async def restore_local_from_icloud(rel):
    local = os.path.join(LOCAL_VAULT, rel)
    icloud = os.path.join(ICLOUD_VAULT, rel)
    history = os.path.join(HISTORY_DIR, rel)
    await async_copy(icloud, local)
    await async_copy(icloud, history)
    file_size = size_or_zero(icloud)
    cooldown_time = BIG_FILE_COOLDOWN if file_size > BIG_FILE_THRESHOLD else COOLDOWN_SECONDS
    cooldowns[rel] = time.time() + cooldown_time

# Safely sync removes a file & dir, logging success or failure without raising exceptions.
def remove_file_safe_sync(path, description):
    try:
        if os.path.exists(path):
            os.remove(path)
            log_success("SUCCESS", f"Removed {description}: {disp(path)}", level="verbose")
    except Exception as e:
        log_error("FAILED", f"Remove {description} {path}: {e}", level="important")

# Safely async removes a file & dir, logging success or failure without raising exceptions.
async def remove_file_safe(path, description):
    try:
        if os.path.exists(path):
            await asyncio.to_thread(os.remove, path)
            log_success("SUCCESS", f"Removed {description}: {disp(path)}", level="verbose")
            dir_path = os.path.dirname(path)
            while dir_path and dir_path not in [LOCAL_VAULT, ICLOUD_VAULT, HISTORY_DIR]:
                if os.path.exists(dir_path) and not os.listdir(dir_path):
                    try:
                        await asyncio.to_thread(os.rmdir, dir_path)
                        log_info("INFO", f"Removed empty directory: {disp(dir_path)}", level="verbose")
                        dir_path = os.path.dirname(dir_path)
                    except OSError:
                        break
                else:
                    break
    except Exception as e:
        log_error("FAILED", f"Remove {description} {path}: {e}", level="important")

# ---------- Startup Duplicate Scanner ----------
def scan_and_clean_duplicates():
    # 1. file_CONFLICT_20260305_123456_123456.md
    # 2. file (1).md, file (2).png
    # 3. .tmp files from incomplete operations
    conflict_pattern = re.compile(r'_CONFLICT_\d{8}_\d{6}_\d{6}')
    icloud_dup_pattern = re.compile(r'\s\(\d+\)\.[^.]+$')
    tmp_pattern = re.compile(r'\.tmp$')
    duplicates_found = []
    log_info("INFO", "Scan for potential conflict or duplicate files before starting sync...", level="important")
    for root_dir in [LOCAL_VAULT, ICLOUD_VAULT, HISTORY_DIR]:
        if not os.path.exists(root_dir):
            continue
        for dirpath, _, filenames in os.walk(root_dir):
            if '.trash' in dirpath.lower().split(os.sep):
                continue
            for f in filenames:
                if conflict_pattern.search(f) or icloud_dup_pattern.search(f) or tmp_pattern.search(f):
                    duplicates_found.append(os.path.join(dirpath, f))
    if not duplicates_found:
        log_success("CLEAN", "No duplicates found. Proceeding with synchronization.", level="important")
        return
    log_error("DANGER", f"Found {len(duplicates_found)} potential conflict or duplicate files. Manual cleanup recommended before sync.", level="important")
    for p in duplicates_found:
        log_warn("DUPLICATE", f"{disp(p)}", level="important")
    log_warn("ACTION", "Do you want to DELETE them WITHOUT RECOVERY before starting synchronization? (Y/n)", level="important")
    sys.stdout.flush()
    ans = input().strip().lower()
    if ans in ['y', 'yes']:
        for p in duplicates_found:
            remove_file_safe_sync(p, "Duplicate/Conflict")
        log_success("CLEAN", "All duplicates removed. Starting synchronization.", level="important")
    else:
        log_warn("INFO", "Skipping duplicate cleanup. Starting synchronization.", level="important")
    print("-" * 75)

# ---------- Build union of all relative file paths ----------
# This function gathers all relative file paths from local, iCloud, and history directories
def gather_all_rel_paths_fast():
    rels = set()
    # Using os.scandir for faster directory traversal and filtering out irrelevant files/directories early to minimize overhead.
    def collect(current_path, base_root):
        try:
            with os.scandir(current_path) as it:
                for entry in it:
                    name_lower = entry.name.lower()
                    if entry.is_dir():
                        # Ignore common system and sync-related directories that are not relevant for syncing, to reduce noise and improve performance
                        if name_lower in IGNORED_DIRS:
                            continue
                        collect(entry.path, base_root)
                    elif entry.is_file():
                        # Ignore temporary and system files that are not relevant for syncing, to reduce noise and improve performance
                        if (name_lower.endswith('.tmp')
                            or name_lower.startswith('._')
                            or name_lower in IGNORED_FILES
                            or 'page-preview' in name_lower):
                            continue
                        rel = os.path.normpath(os.path.relpath(entry.path, base_root))
                        if is_ignored(rel):
                            continue
                        rels.add(rel)
        except FileNotFoundError:
            pass
    collect(LOCAL_VAULT, LOCAL_VAULT)
    collect(ICLOUD_VAULT, ICLOUD_VAULT)
    collect(HISTORY_DIR, HISTORY_DIR)
    return rels

# ---------- Main per-file sync logic ----------
# Core sync logic for a single file
async def sync_file(rel_path, state):
    now = time.time()
    if rel_path in cooldowns and cooldowns[rel_path] > now:
        return
    local = os.path.join(LOCAL_VAULT, rel_path)
    icloud = os.path.join(ICLOUD_VAULT, rel_path)
    history = os.path.join(HISTORY_DIR, rel_path)
    L_exists = safe_exists(local)
    C_exists = safe_exists(icloud)
    H_exists = safe_exists(history)

    # ------------- CASE: Nothing exists anywhere -------------
    if not L_exists and not C_exists:
        if H_exists:
            log_warn("REMOVING HISTORY", f"{colored('No local', Fore.RED)} & {colored('No iCloud', Fore.RED)} for {disp(rel_path)}", level="important")
            await remove_file_safe(history, "history")
        return
    # Helper to re-evaluate hashes forcefully after stability window
    async def recheck_hashes():
        await asyncio.sleep(STABILITY_WINDOW)
        Lh = await get_cached_hash(local, 'L', rel_path, state, force=True) if safe_exists(local) else None
        Ch = await get_cached_hash(icloud, 'C', rel_path, state, force=True) if safe_exists(icloud) else None
        Hh = await get_cached_hash(history, 'H', rel_path, state, force=True) if safe_exists(history) else None
        return Lh, Ch, Hh

    # ------------- CASE: Local missing, history & icloud EXIST to DELETE both -------------
    if (not L_exists) and C_exists and H_exists:
        log_warn("DELETE", f"{colored('Local missing', Fore.RED)}, stabilizing: history & iCloud present for {disp(rel_path)}", level="verbose")
        Lh, Ch, Hh = await recheck_hashes()
        if Ch is not None and Hh is not None and Ch == Hh:
            log_custom([" ←","⚫"], [Fore.RED, Fore.RED], "DELETE", f"{colored('Removing from iCloud', Fore.CYAN)} & history for {disp(rel_path)}", rel_path, level="verbose")
            await remove_file_safe(icloud, "iCloud")
            await remove_file_safe(history, "history")
        else:
            log_custom([" ↓","⚪"], [Fore.CYAN, Fore.CYAN], "PULL", f"{colored('Restoring to local', Fore.GREEN)} from iCloud, local changed vs history for {disp(rel_path)}", rel_path, level="verbose")
            await restore_local_from_icloud(rel_path)
        return

    # ------------- CASE: iCloud missing, history & local EXIST to DELETE local & history -------------
    if (not C_exists) and L_exists and H_exists:
        log_warn("DELETE",f"{colored('iCloud missing', Fore.RED)}, stabilizing: local & history present for {disp(rel_path)}", level="verbose")
        Lh, Ch, Hh = await recheck_hashes()
        if Lh is not None and Hh is not None and Lh == Hh:
            log_custom([" ←","⚫"], [Fore.RED, Fore.RED], "DELETE", f"{colored('Removing local', Fore.RED)} & history for {disp(rel_path)}", rel_path, level="verbose")
            await remove_file_safe(local, "local")
            await remove_file_safe(history, "history")
        else:
            log_custom([" ↑","⚪"], [Fore.GREEN, Fore.GREEN], "PUSH", f"Local changed vs history for {disp(rel_path)} -> pushing local to iCloud", rel_path, level="verbose")
            await push_local_to_icloud(rel_path)
        return

    # ------------- CASE: New creation (local exists, no history nor icloud) -------------
    if L_exists and (not C_exists) and (not H_exists):
        log_custom([" →","⚪"], [Fore.LIGHTBLACK_EX, Fore.GREEN], "NEW", f"{colored('Local-only', Fore.GREEN)}, stabilizing: new file detected {disp(rel_path)}", rel_path, level="verbose")
        Lh, Ch, Hh = await recheck_hashes()
        local_size = size_or_zero(local)
        if Lh is None:
            log_info("SKIP", f"After stabilize local missing or unreadable for {disp(rel_path)}", level="verbose")
            return
        if local_size < TINY_THRESHOLD:
            log_info("SKIP", f"Local file too small ({local_size} bytes), deferring: {disp(rel_path)}", level="verbose")
            return
        log_custom([" ↑","⚪"], [Fore.GREEN, Fore.GREEN], "PUSH", f"{colored('Pushing to iCloud', Fore.CYAN)} & seeding history from local for {disp(rel_path)}", rel_path, level="verbose")
        await push_local_to_icloud(rel_path)
        return

    # ------------- CASE: New creation (icloud exists, no local nor history) -------------
    if C_exists and (not L_exists) and (not H_exists):
        log_custom([" →","⚪"], [Fore.LIGHTBLACK_EX, Fore.BLUE], "NEW", f"{colored('iCloud-only', Fore.CYAN)}, stabilizing: new file detected {disp(rel_path)}", rel_path, level="verbose")
        Lh, Ch, Hh = await recheck_hashes()
        icloud_size = size_or_zero(icloud)
        if Ch is None:
            log_info("SKIP", f"After stabilize iCloud missing or unreadable for {disp(rel_path)}", level="verbose")
            return
        if icloud_size < TINY_THRESHOLD:
            log_info("SKIP", f"iCloud file too small ({icloud_size} bytes), deferring: {disp(rel_path)}", level="verbose")
            return
        log_custom([" ↓","⚪"], [Fore.CYAN, Fore.CYAN], "PULL", f"{colored('Restoring to local', Fore.GREEN)} & seeding history from iCloud for {disp(rel_path)}", rel_path, level="verbose")
        await restore_local_from_icloud(rel_path)
        return

    # ------------- At this point both sides exist OR we have mixed states -------------
    ensure_dir(os.path.dirname(history))

    # Fast cached hash checks
    L = await get_cached_hash(local, 'L', rel_path, state) if safe_exists(local) else None
    C = await get_cached_hash(icloud, 'C', rel_path, state) if safe_exists(icloud) else None
    H = await get_cached_hash(history, 'H', rel_path, state) if safe_exists(history) else None

    # If history missing but both sides exist, seed history conservatively after stabilization
    if H is None and (L is not None or C is not None):
        log_info("HISTORY MISSING", f"Waiting to seed history for {disp(rel_path)}")
        Lh, Ch, Hh = await recheck_hashes()
        # Safe seeding strategy:
        if Lh is not None and Ch is not None:
            if Lh == Ch:
                # Files are identical, we can safely seed history
                await async_copy(local, history)
                H = Lh
                log_info("HISTORY", f"Initialized history (identical files) for {disp(rel_path)}", level="verbose")
            else:
                # Conflict at start with no history
                log_warn("HISTORY", f"Local and iCloud differ for {disp(rel_path)}!", level="verbose")
                local_m = safe_mtime(local)
                icloud_m = safe_mtime(icloud)
                # Keep the newer version as the main one, and create a conflict duplicate of the older version for recovery.
                if local_m >= icloud_m:
                    log_warn("CONFLICT", f"{colored('Local is newer', Fore.YELLOW)}: {disp(rel_path)}", level="important")
                    await create_conflict_duplicate(icloud)
                    await push_local_to_icloud(rel_path)
                    H = Lh
                else:
                    log_warn("CONFLICT", f"{colored('iCloud is newer', Fore.YELLOW)}: {disp(rel_path)}", level="important")
                    await create_conflict_duplicate(local)
                    await restore_local_from_icloud(rel_path)
                    H = Ch
                return
        # If we can't read one side, we can still seed history from the other side if it looks valid (not tiny)
        elif Lh is not None and size_or_zero(local) >= min_seed_size(rel_path):
            await async_copy(local, history)
            H = Lh
            log_info("HISTORY", f"Initialized {colored('history from local', Fore.GREEN)} for {disp(rel_path)}", level="verbose")
        elif Ch is not None and size_or_zero(icloud) >= min_seed_size(rel_path):
            await async_copy(icloud, history)
            H = Ch
            log_info("HISTORY", f"Initialized {colored('history from iCloud', Fore.CYAN)} for {disp(rel_path)}", level="verbose")
        else:
            # Could be a permanent lock
            if size_or_zero(local) > 0 or size_or_zero(icloud) > 0:
                log_error("FAILED", f"Files exist but are unreadable for {disp(rel_path)}; retrying next pass", level="important")
            else:
                log_info("SKIP", f"History seeding skipped for {disp(rel_path)}; will retry next pass", level="verbose")
            return

    # CASE A: Identical
    if L == C == H:
        return

    # CASE B: Local changed
    if L is not None and H is not None and (L != H) and (C == H):
        log_custom([" ↑","⚪"], [Fore.GREEN, Fore.GREEN], "PUSH", f"{colored('Local changed', Fore.GREEN)}, pushing local for {disp(rel_path)}", rel_path, level="verbose")
        await push_local_to_icloud(rel_path)
        return

    # CASE C: iCloud changed
    if C is not None and H is not None and (C != H) and (L == H):
        log_custom([" ↓","⚪"], [Fore.CYAN, Fore.CYAN], "PULL", f"{colored('iCloud changed', Fore.CYAN)}, restoring local for {disp(rel_path)}", rel_path, level="verbose")
        await restore_local_from_icloud(rel_path)
        return

    # CASE D: Both changed (RARE)
    log_warn("CONFLICT", f"Both changed (stabilizing) for {disp(rel_path)} {STABILIZE_WAIT}s", level="important")
    await asyncio.sleep(STABILIZE_WAIT)

    L2 = await get_cached_hash(local, 'L', rel_path, state, force=True) if safe_exists(local) else None
    C2 = await get_cached_hash(icloud, 'C', rel_path, state, force=True) if safe_exists(icloud) else None
    # H2 is not relevant

    # Conflict resolution strategy
    if L2 is not None and L2 != L:
        log_warn("CONFLICT", f"{colored('Local still changing', Fore.YELLOW)}, choose local: {disp(rel_path)}", level="important")
        await create_conflict_duplicate(icloud)
        await push_local_to_icloud(rel_path)
        return
    if C2 is not None and C2 != C:
        log_warn("CONFLICT", f"{colored('iCloud still changing', Fore.YELLOW)}, choose iCloud: {disp(rel_path)}", level="important")
        await create_conflict_duplicate(local)
        await restore_local_from_icloud(rel_path)
        return
    if not safe_exists(local):
        log_custom([" ↓","🟡"], [Fore.CYAN, Fore.YELLOW], "PULL", f"{colored('Local vanished', Fore.YELLOW)} during stabilize, {colored('restoring from iCloud', Fore.CYAN)}: {disp(rel_path)}", rel_path, level="verbose")
        await restore_local_from_icloud(rel_path)
        return
    if not safe_exists(icloud):
        log_custom([" ↑","🟡"], [Fore.GREEN, Fore.YELLOW], "PUSH", f"{colored('iCloud vanished', Fore.YELLOW)} during stabilize, {colored('restoring from local', Fore.GREEN)}: {disp(rel_path)}", rel_path, level="verbose")
        await push_local_to_icloud(rel_path)
        return

    # Fallback to timestamps
    local_m = safe_mtime(local)
    icloud_m = safe_mtime(icloud)
    if local_m >= icloud_m:
        log_info("CONFLICT", f"{colored('Local is newer', Fore.YELLOW)}, push local: {disp(rel_path)}", level="important")
        await create_conflict_duplicate(icloud)
        await push_local_to_icloud(rel_path)
    else:
        log_info("CONFLICT", f"{colored('iCloud is newer', Fore.YELLOW)}, pull iCloud: {disp(rel_path)}", level="important")
        await create_conflict_duplicate(local)
        await restore_local_from_icloud(rel_path)

# ---------- Concurrency wrapper ----------
# Multiple files can be processed in parallel, but we need to limit concurrent I/O
async def sync_wrapper(rel, state):
    try:
        async with io_semaphore:
            await sync_file(rel, state)
    except Exception as e:
        log_error("ERROR", f"Error syncing {disp(rel)}: {e}", level="important")
        log_error("TRACEBACK", traceback.format_exc(), level="important")
    finally:
        active_tasks.discard(rel)

# ---------- Main ----------
async def main():
    global state_dirty, CURRENT_LOG_FILE

    # ------------- Initialize logging -------------
    validate_config()
    await cleanup_old_logs(LOG_RETENTION)
    start_time_str = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    CURRENT_LOG_FILE = os.path.join(LOGS_DIR, f"sync_{start_time_str}.log")

    # ------------- Initialize state and log startup -------------
    mode_str = "DAEMON MODE" if RUN_CONTINUOUSLY else "ONE-SHOT MODE"
    console_startup(mode_str)
    scan_and_clean_duplicates()
    state = load_state()
    last_save_time = time.time()

    # ------------- Main loop -------------
    try:
        while True:
            try:
                # 1. Memory leak prevention: Cleanup old cooldowns
                now = time.time()
                expired = [k for k, v in cooldowns.items() if v <= now]
                for k in expired:
                    del cooldowns[k]

                # 2. Fast scan using os.scandir
                rel_paths = gather_all_rel_paths_fast()
                # Prevents unnecessary state loading and saves resources
                rel_paths.update(k for k, v in state.items() if v)
                tasks_to_await = []

                # 3. Process concurrently
                active_rels = [
                    r for r in sorted(rel_paths)
                    if r not in active_tasks
                ]
                if active_rels and not RUN_CONTINUOUSLY:
                    console_header(len(active_rels))
                for rel in active_rels:
                    active_tasks.add(rel)
                    task = asyncio.create_task(sync_wrapper(rel, state))
                    tasks_to_await.append(task)

                # ------------- One-shot mode: wait for tasks and exit -------------
                if not RUN_CONTINUOUSLY:
                    if tasks_to_await:
                        log_info("INFO",f"One-shot sync: waiting for {len(tasks_to_await)} tasks to complete...", level="normal")
                        await asyncio.gather(*tasks_to_await)
                    else:
                        log_info("INFO", "Nothing to sync.", level="normal")
                    empty_keys = [k for k, v in state.items() if not v]
                    for k in empty_keys:
                        del state[k]
                    save_state(state)
                    _flush_log_buffer()
                    log_success("DONE", "One-shot synchronization complete.", level="normal")
                    break

                # ------------- Daemon mode: Save state periodically and continue -------------
                if not tasks_to_await:
                    console_idle()
                # 4. Cleanup state entries for files that no longer exist
                empty_keys = [k for k, v in state.items() if not v]
                for k in empty_keys:
                    del state[k]
                    state_dirty = True

                # 5. Save state if dirty, debounced to every 5 seconds
                now_t = time.time()
                if state_dirty and now_t - last_save_time > 5:
                    save_state(state)
                    _flush_log_buffer()
                    state_dirty = False
                    last_save_time = now_t
                elif now_t - last_save_time > 5:
                    _flush_log_buffer()
                    last_save_time = now_t

            except Exception as outer:
                log_error("ERROR", f"Unexpected error in main loop: {outer}", level="important")
                log_error("TRACEBACK", traceback.format_exc(), level="important")
            await asyncio.sleep(POLL_INTERVAL)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log_warn("INFO", "Shutdown requested, saving state...", level="important")
        save_state(state)
        _flush_log_buffer()
        log_success("DONE", "Graceful shutdown complete.", level="important")
        sys.exit(0)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass