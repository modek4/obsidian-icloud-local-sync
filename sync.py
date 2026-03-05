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

# ----------------- CONFIG -----------------
# Windows local path
LOCAL_VAULT = r"C:\Obsidian\Vault"
# Windows iCloud path
ICLOUD_VAULT = r"C:\Users\user\iCloudDrive\iCloud~md~obsidian"
# History changelog path
HISTORY_DIR = r"C:\Obsidian\History"
# Log files directory
LOGS_DIR = r"C:\Obsidian\Logs"
# Run mode: one-shot vs continuous daemon
RUN_CONTINUOUSLY = False
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
# ------------------------------------------

# rel_path -> timestamp until which file is on cooldown
cooldowns = {}
# tracks currently processing rel_paths to avoid parallel overlapping
active_tasks = set()
# flag to know when to save the state file
state_dirty = False
# limit concurrent IO operations to avoid overwhelming the disk
io_semaphore = asyncio.Semaphore(50)

# ---------- Path helpers ----------

def disp(path):
    # Returns a shortened display version of the path for logs, improving readability while keeping key info.
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
def strip_ansi(text):
    # Delete colors for clean log files, using Coloramas output
    ansi_escape = re.compile(r'\x1b\[([0-9]{1,3}(;[0-9]{1,2})?)?[mGK]')
    return ansi_escape.sub('', text)

# Log to file
def write_to_log_file(msg_type, msg):
    # Appends a log entry to the current log file with timestamp and message type. Strips ANSI color codes for clean logs.
    if not CURRENT_LOG_FILE:
        return
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    clean_msg = strip_ansi(msg)
    log_line = f"[{timestamp}] [{msg_type}] {clean_msg}\n"
    try:
        # 'a' means append mode
        with open(CURRENT_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_line)
    except Exception as e:
        print(Fore.RED + f"[ERROR] Failed writing to log file: {e}" + Style.RESET_ALL)

# Console info and log file entry
def log_info(msg):
    print(Fore.CYAN + "[INFO] " + Style.RESET_ALL + msg)
    write_to_log_file("INFO", msg)

# Console warning and log file entry
def log_warn(msg):
    print(Fore.YELLOW + "[WARN] " + Style.RESET_ALL + msg)
    write_to_log_file("WARN", msg)

# Console error and log file entry
def log_error(msg):
    print(Fore.RED + "[ERROR] " + Style.RESET_ALL + msg)
    write_to_log_file("ERROR", msg)

# Console success and log file entry
def log_success(msg):
    print(Fore.GREEN + "[OK] " + Style.RESET_ALL + msg)
    write_to_log_file("OK", msg)

# Console action and log file entry (for operations like copy, delete, etc.)
def log_action(msg):
    print(Fore.MAGENTA + "[ACTION] " + Style.RESET_ALL + msg)
    write_to_log_file("ACTION", msg)

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
        return False

# Wrappers to avoid exceptions when files are locked or deleted during checks
def size_or_zero(path):
    try:
        return os.path.getsize(path)
    except Exception:
        return 0

# Better mtime
def safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0

# State management for caching file hashes and mtimes to minimize disk I/O.
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log_warn(f"Failed loading state file: {e}")
        return {}

# Cleans up state by removing entries for files that no longer exist
def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log_warn(f"Failed saving state file: {e}")

# ----------- Hashing (async & cached) ------------
# Asynchronously computes SHA-256 hash of a file with retries to handle transient access issues.
async def hash_file(path, max_retries=6):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    attempt = 0
    backoff = 0.05
    while True:
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
                log_error(f"hash_file giving up on {path}: {e}")
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
    log_action(f"Copying {disp(src)} -> {disp(dst)}")
    ensure_dir(os.path.dirname(dst))
    tmp = dst + ".tmp"
    try:
        # Run blocking copy in thread so loop remains responsive
        await asyncio.to_thread(shutil.copy2, src, tmp)
    except Exception as e:
        log_error(f"Failed to write tmp file {tmp}: {e}")
        raise
    backoff = initial_backoff
    attempt = 0
    while True:
        try:
            if os.path.exists(dst):
                set_normal_attributes(dst)
            # First try os.replace (atomic same-drive)
            os.replace(tmp, dst)
            log_success(f"Updated: {dst}")
            return
        except PermissionError as e:
            attempt += 1
            if attempt >= max_retries:
                log_warn("Max retries reached - trying Win32 MoveFileEx fallback")
                try:
                    ok = MoveFileExW(tmp, dst, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)
                    if ok:
                        log_success(f"MoveFileEx succeeded: {dst}")
                        return
                    else:
                        log_error(f"MoveFileEx failed (err {ctypes.get_last_error()}).")
                except Exception as exc:
                    log_error(f"MoveFileEx exception: {exc}")
                # Final brute-force attempt
                try:
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.replace(tmp, dst)
                    log_success(f"Forced replace succeeded after removing destination: {dst}")
                    return
                except Exception as exc:
                    log_error(f"Final forced replace failed: {exc}")
                    try:
                        if os.path.exists(tmp): os.remove(tmp)
                    except Exception: pass
                    raise PermissionError(f"Unable to replace {dst}") from exc
            set_normal_attributes(dst)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.8, 5.0)
        except Exception as unexpected:
            log_error(f"Unexpected error during replace: {unexpected}")
            try:
                if os.path.exists(tmp): os.remove(tmp)
            except Exception: pass
            raise

# Creates a conflict duplicate of the given file by copying it with a timestamped name.
async def create_conflict_duplicate(path):
    base, ext = os.path.splitext(path)
    conflict = f"{base}_CONFLICT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
    log_warn(f"Creating conflict duplicate: {conflict}")
    try:
        await asyncio.to_thread(shutil.copy2, path, conflict)
    except Exception as e:
        log_error(f"Failed to create conflict duplicate {conflict}: {e}")

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

# Safely removes a file & dir, logging success or failure without raising exceptions.
def remove_file_safe(path, description):
    try:
        if os.path.exists(path):
            os.remove(path)
            log_success(f"Removed {description}: {disp(path)}")
            dir_path = os.path.dirname(path)
            while dir_path and dir_path not in [LOCAL_VAULT, ICLOUD_VAULT, HISTORY_DIR]:
                if os.path.exists(dir_path) and not os.listdir(dir_path):
                    try:
                        os.rmdir(dir_path)
                        log_info(f"Removed empty directory: {disp(dir_path)}")
                        dir_path = os.path.dirname(dir_path)
                    except OSError:
                        break
                else:
                    break
    except Exception as e:
        log_error(f"Failed to remove {description} {path}: {e}")

# ---------- Startup Duplicate Scanner ----------
def scan_and_clean_duplicates():
    # 1. file_CONFLICT_20260305_123456_123456.md
    # 2. file (1).md, file (2).png
    conflict_pattern = re.compile(r'_CONFLICT_\d{8}_\d{6}')
    icloud_dup_pattern = re.compile(r'\s\(\d+\)\.[^.]+$')
    duplicates_found = []
    log_info("Scan for potential conflict/duplicate files before starting sync...")
    for root_dir in [LOCAL_VAULT, ICLOUD_VAULT, HISTORY_DIR]:
        if not os.path.exists(root_dir): 
            continue
        for dirpath, _, filenames in os.walk(root_dir):
            if '.trash' in dirpath.lower().split(os.sep):
                continue
            for f in filenames:
                if conflict_pattern.search(f) or icloud_dup_pattern.search(f):
                    duplicates_found.append(os.path.join(dirpath, f))
    if not duplicates_found:
        log_info("No duplicates found. Proceeding with synchronization.")
        return
    print(Fore.RED + f"\n[WARNING] Found {len(duplicates_found)} conflicting/duplicate files:" + Style.RESET_ALL)
    for p in duplicates_found:
        print(Fore.YELLOW + f" - {disp(p)}" + Style.RESET_ALL)
    print(Fore.CYAN + "\nDo you want to DELETE them WITHOUT RECOVERY before starting synchronization? (Y/n): " + Style.RESET_ALL, end="")
    sys.stdout.flush()
    ans = input().strip().lower()
    if ans in ['y', 'yes']:
        for p in duplicates_found:
            remove_file_safe(p, "Duplicate/Conflict")
        log_info("All duplicates removed. Starting synchronization.")
    else:
        log_info("Skipping duplicate cleanup. Starting synchronization.")
    print("-" * 50)

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
                        if name_lower in ['.trash', '.fseventsd', '.spotlight-v100', '.apdisk']:
                            continue
                        collect(entry.path, base_root)
                    elif entry.is_file():
                        # Ignore temporary and system files that are not relevant for syncing, to reduce noise and improve performance
                        if (name_lower.endswith('.tmp') or name_lower.startswith('._') or name_lower in ['.ds_store', '.trash'] or 'page-preview' in name_lower or 'workspace' in name_lower):
                            continue
                        rel = os.path.relpath(entry.path, base_root)
                        rels.add(os.path.normpath(rel))
        except FileNotFoundError:
            pass
    collect(LOCAL_VAULT, LOCAL_VAULT)
    collect(ICLOUD_VAULT, ICLOUD_VAULT)
    collect(HISTORY_DIR, HISTORY_DIR)
    return {r.lstrip(os.sep) for r in rels}

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
            log_warn(f"\n[DELETE] No local & no iCloud for {disp(rel_path)} -> removing history")
            remove_file_safe(history, "history")
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
        log_warn(f"\n[POTENTIAL DELETE] Local missing; history+icloud present for {disp(rel_path)} -> stabilizing")
        Lh, Ch, Hh = await recheck_hashes()
        if Ch is not None and Hh is not None and Ch == Hh:
            log_warn(f"[DELETE CONFIRMED] Removing iCloud & history for {disp(rel_path)}")
            remove_file_safe(icloud, "iCloud")
            remove_file_safe(history, "history")
        else:
            log_warn(f"[RESTORE] iCloud changed vs history for {disp(rel_path)} -> restoring local")
            await restore_local_from_icloud(rel_path)
        return

    # ------------- CASE: iCloud missing, history & local EXIST to DELETE local & history -------------
    if (not C_exists) and L_exists and H_exists:
        log_warn(f"\n[POTENTIAL DELETE] iCloud missing; local+history present for {disp(rel_path)} -> stabilizing")
        Lh, Ch, Hh = await recheck_hashes()
        if Lh is not None and Hh is not None and Lh == Hh:
            log_warn(f"[DELETE CONFIRMED] Removing local & history for {disp(rel_path)}")
            remove_file_safe(local, "local")
            remove_file_safe(history, "history")
        else:
            log_warn(f"[PUSH] Local changed vs history for {disp(rel_path)} -> pushing local to iCloud")
            await push_local_to_icloud(rel_path)
        return

    # ------------- CASE: New creation (local exists, no history nor icloud) -------------
    if L_exists and (not C_exists) and (not H_exists):
        log_warn(f"\n[CREATE] Local-only new file detected {disp(rel_path)} -> stabilizing")
        Lh, Ch, Hh = await recheck_hashes()
        local_size = size_or_zero(local)
        if Lh is None:
            log_info(f"[CREATE] After stabilize local missing or unreadable for {disp(rel_path)} -> skip")
            return
        if local_size < TINY_THRESHOLD:
            log_info(f"[CREATE] Local file appears tiny ({local_size} bytes); deferring {disp(rel_path)}")
            return
        log_info(f"[CREATE] Seeding history & pushing to iCloud for {disp(rel_path)}")
        await push_local_to_icloud(rel_path)
        return

    # ------------- CASE: New creation (icloud exists, no local nor history) -------------
    if C_exists and (not L_exists) and (not H_exists):
        log_warn(f"\n[CREATE] iCloud-only new file detected {disp(rel_path)} -> stabilizing")
        Lh, Ch, Hh = await recheck_hashes()
        icloud_size = size_or_zero(icloud)
        if Ch is None:
            log_info(f"[CREATE] After stabilize iCloud unreadable/missing for {disp(rel_path)} -> skip")
            return
        if icloud_size < TINY_THRESHOLD:
            log_info(f"[CREATE] iCloud file appears tiny ({icloud_size} bytes); deferring {disp(rel_path)}")
            return
        log_info(f"[CREATE] Restoring local & seeding history from iCloud for {disp(rel_path)}")
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
        log_info(f"[HISTORY MISSING] Waiting to seed history for {disp(rel_path)}")
        Lh, Ch, Hh = await recheck_hashes()
        if Lh is not None and size_or_zero(local) >= (TINY_THRESHOLD if 'obsidian' not in rel_path.lower() else 1):
            await async_copy(local, history)
            H = Lh
            log_info(f"Initialized history from local for {disp(rel_path)}")
        elif Ch is not None and size_or_zero(icloud) >= TINY_THRESHOLD:
            await async_copy(icloud, history)
            H = Ch
            log_info(f"Initialized history from iCloud for {disp(rel_path)}")
        else:
            log_info(f"History seeding skipped for {disp(rel_path)}; will retry next pass")
            return

    # CASE A: Identical
    if L == C == H:
        return

    # CASE B: Local changed
    if L is not None and H is not None and (L != H) and (C == H):
        log_info(f"\n[SYNC] Local changed -> pushing local for {disp(rel_path)}")
        await push_local_to_icloud(rel_path)
        return

    # CASE C: iCloud changed
    if C is not None and H is not None and (C != H) and (L == H):
        log_info(f"\n[SYNC] iCloud changed -> restoring local for {disp(rel_path)}")
        await restore_local_from_icloud(rel_path)
        return

    # CASE D: Both changed (RARE)
    log_warn(f"\nCase D (both changed) for {disp(rel_path)} -> stabilizing {STABILIZE_WAIT}s")
    await asyncio.sleep(STABILIZE_WAIT)

    L2 = await get_cached_hash(local, 'L', rel_path, state, force=True) if safe_exists(local) else None
    C2 = await get_cached_hash(icloud, 'C', rel_path, state, force=True) if safe_exists(icloud) else None
    # H2 is not relevant

    # Conflict resolution strategy
    if L2 is not None and L2 != L:
        log_warn("Local still changing — choose local")
        await create_conflict_duplicate(icloud)
        await push_local_to_icloud(rel_path)
        return
    if C2 is not None and C2 != C:
        log_warn("iCloud still changing — choose iCloud")
        await create_conflict_duplicate(local)
        await restore_local_from_icloud(rel_path)
        return

    # Fallback to timestamps
    local_m = safe_mtime(local)
    icloud_m = safe_mtime(icloud)
    if local_m >= icloud_m:
        log_warn("Resolving conflict: local newer")
        await create_conflict_duplicate(icloud)
        await push_local_to_icloud(rel_path)
    else:
        log_warn("Resolving conflict: iCloud newer")
        await create_conflict_duplicate(local)
        await restore_local_from_icloud(rel_path)

# ---------- Concurrency wrapper ----------
# Multiple files can be processed in parallel, but we need to limit concurrent I/O
async def sync_wrapper(rel, state):
    try:
        async with io_semaphore:
            await sync_file(rel, state)
    except Exception as e:
        log_error(f"Error syncing {disp(rel)}: {e}")
        write_to_log_file("TRACEBACK", traceback.format_exc())
        traceback.print_exc()
    finally:
        active_tasks.discard(rel)

# ---------- Main ----------
async def main():
    global state_dirty, CURRENT_LOG_FILE

    # ------------- Initialize logging -------------
    ensure_dir(LOGS_DIR)
    start_time_str = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    CURRENT_LOG_FILE = os.path.join(LOGS_DIR, f"sync_{start_time_str}.log")

    # ------------- Initialize state and log startup -------------
    mode_str = "DAEMON MODE" if RUN_CONTINUOUSLY else "ONE-SHOT MODE"
    log_info(f"Starting Obsidian Sync ({mode_str})")
    ensure_dir(HISTORY_DIR)
    scan_and_clean_duplicates()
    state = load_state()
    last_save_time = time.time()

    # ------------- Main loop -------------
    while True:
        try:
            # 1. Memory leak prevention: Cleanup old cooldowns
            now = time.time()
            expired = [k for k, v in cooldowns.items() if v <= now]
            for k in expired:
                del cooldowns[k]

            # 2. Fast scan using os.scandir
            rel_paths = gather_all_rel_paths_fast()
            rel_paths.update(state.keys())
            tasks_to_await = []

            # 3. Process concurrently
            for rel in sorted(rel_paths):
                if '.trash' in rel.lower().split(os.sep):
                    continue
                if rel in active_tasks:
                    continue
                active_tasks.add(rel)
                task = asyncio.create_task(sync_wrapper(rel, state))
                tasks_to_await.append(task)

            # ------------- One-shot mode: wait for tasks and exit -------------
            if not RUN_CONTINUOUSLY:
                if tasks_to_await:
                    log_info(f"One-shot sync: waiting for {len(tasks_to_await)} tasks to complete...")
                    await asyncio.gather(*tasks_to_await)
                else:
                    log_info("Nothing to sync. Exiting.")
                empty_keys = [k for k, v in state.items() if not v]
                for k in empty_keys:
                    del state[k]
                save_state(state)
                log_success("One-shot sync completed. Exiting.")
                break

            # ------------- Daemon mode: Save state periodically and continue -------------
            # 4. Cleanup state entries for files that no longer exist
            empty_keys = [k for k, v in state.items() if not v]
            for k in empty_keys:
                del state[k]
                state_dirty = True

            # 5. Save state if dirty, debounced to every 5 seconds
            if state_dirty and time.time() - last_save_time > 5:
                save_state(state)
                state_dirty = False
                last_save_time = time.time()

        except Exception as outer:
            log_error(f"Unexpected error during scan: {outer}")
            write_to_log_file("TRACEBACK", traceback.format_exc())
            traceback.print_exc()
        await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
