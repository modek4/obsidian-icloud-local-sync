# Changelog

## [1.1.0] - 2026-04-09

### Summary
- File duplicate protection added via kernel32
- Added tests `./src/obsidian_sync/tests`

### Added
- `ICloudStatusChecker` reads Windows file attributes via `GetFileAttributesW` (kernel32) to determine iCloud sync state
- `ICloudSyncState` enum with states: `LOCAL`, `PINNED`, `PENDING`, `DOWNLOADING`, `CLOUD_ONLY`, `UNKNOWN`
- iCloud status guard defers processing of files that are not yet locally available
- If local file has changed relative to history (`L != H`), push proceeds even over a cloud-only placeholder
- `RtlSetProcessPlaceholderCompatibilityMode(PHCM_EXPOSE_PLACEHOLDERS)` ensures placeholder attributes are not hidden by the OS
- New config values: `user_interface: true` and `check_icloud_status: true`

## [1.0.1] - 2026-04-05

### Summary
- Improved reliability, startup safety, logging, and error handling across the sync flow.
- Added safer config validation, more robust file operations, better duplicate/state handling, and cleaner async runtime behavior.

### Added
- Handle the case where both files have stabilized but still differ (L2 == L and C2 == C but L != C)

## Fixed
- Config: `validate()` returns errors (no `sys.exit`); added path equality guards & `max_concurrent_io`.
- Engine: `gather_rel_paths()` threaded; `sys.exit` changed to `ValueError`; logs init before config check.
- I/O: Cross-platform Win32 imports; clear `.tmp` before copy; race-condition guard in file removal.
- State & Duplicates: Atomic `save_state()`; scan `.tmp` files; run duplicate scan before main loop.
- Logging: Flush before crashes; clear buffer only on success; minimum log retention limit.
- Misc: Added type hints, docs, and YAML read guard.

## [1.0.0] - 2026-03-31

### Summary
- Refactored the project from one monolithic script into a package-based architecture.
- Preserved the original three-way sync behavior while making the code easier to maintain and extend.

### Added
- Pip-installable packaging via `pyproject.toml`
- Source layout under `src/obsidian_sync`
- Split `sync.py` into modules by responsibility:
	- `config.py` (config loading/validation)
	- `logger.py` (console/file logging)
	- `hasher.py` (hashing + state cache)
	- `disk_io.py` (atomic copy/delete and Windows fallbacks)
	- `duplicates.py` (startup duplicate scan)
	- `sync_engine.py` (main loop + sync rules)
- YAML configuration file (`config.yaml`)
- CLI entry point: `obsidian-sync --config config.yaml`

### Kept Behavior
- Three-way sync model (`Local`, `iCloud`, `History`)
- Conflict handling with duplicate backup files
- Stabilization windows and cooldowns to avoid thrashing
- One-shot and daemon modes


## [0.10.0] - Contributed by modek4 (Merged PR)

### Summary
- Major functional upgrade over original baseline while still in single-script form.
- Focused on performance, safety, and operational robustness.

### Added
- Hash caching via mtime/size to skip unchanged files and reduce disk I/O
- Concurrent file processing with `asyncio.create_task` and semaphore limiting
- Structured logging system with console levels (`quiet`/`normal`/`verbose`) and file output with log rotation
- Configurable ignore patterns, ignored directories, and ignored files for filtering
- Big file cooldown (`BIG_FILE_COOLDOWN`) to prevent thrashing on large attachments
- Startup duplicate scanner that detects `_CONFLICT_*`, iCloud `(1)` copies, and stale `.tmp` files
- One-shot run mode alongside daemon mode (`RUN_CONTINUOUSLY` toggle)
- Config validation at startup with early exit on misconfiguration
- Graceful shutdown with state persistence on `Ctrl+C`
- Smarter history seeding with conflict detection when both sides differ


## [0.9.0] - Original Baseline

### Summary
- Original async three-way sync implementation in `sync.py`.
- Single-file, working baseline for Local/iCloud/History synchronization.

### Core Functionality
- Three-way sync model (Local, iCloud, History)
- Hash-based change detection
- Conflict handling with mtime fallback
- Atomic copy/replace strategy with Windows fallback
- Cooldowns and stabilization windows
- Asyncio support for allowing CPU to be idle