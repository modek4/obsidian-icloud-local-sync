import os
import sys
import time
import asyncio
import traceback

from colorama import Fore

from .logger import colored
from .disk_io import safe_exists, size_or_zero, safe_mtime, ensure_dir


class SyncEngine:
    def __init__(self, config, logger, hasher, disk_io, duplicates):
        self.config = config
        self.log = logger
        self.hasher = hasher
        self.io = disk_io
        self.duplicates = duplicates
        self.cooldowns: dict[str, float] = {}
        self.active_tasks: set[str] = set()
        self.io_semaphore = asyncio.Semaphore(50)

    # ── Core file operations ─────────────────────────────────────

    async def push_to_icloud(self, rel):
        local = os.path.join(self.config.local_vault, rel)
        icloud = os.path.join(self.config.icloud_vault, rel)
        history = os.path.join(self.config.history_dir, rel)
        await self.io.async_copy(local, icloud)
        await self.io.async_copy(local, history)
        size = size_or_zero(local)
        cd = self.config.big_file_cooldown if size > self.config.big_file_threshold else self.config.cooldown_seconds
        self.cooldowns[rel] = time.time() + cd

    async def restore_from_icloud(self, rel):
        local = os.path.join(self.config.local_vault, rel)
        icloud = os.path.join(self.config.icloud_vault, rel)
        history = os.path.join(self.config.history_dir, rel)
        await self.io.async_copy(icloud, local)
        await self.io.async_copy(icloud, history)
        size = size_or_zero(icloud)
        cd = self.config.big_file_cooldown if size > self.config.big_file_threshold else self.config.cooldown_seconds
        self.cooldowns[rel] = time.time() + cd

    # ── Path gathering ───────────────────────────────────────────

    def gather_rel_paths(self):
        rels = set()
        cfg = self.config

        def collect(current_path, base_root):
            try:
                with os.scandir(current_path) as it:
                    for entry in it:
                        name_lower = entry.name.lower()
                        if entry.is_dir():
                            if name_lower in cfg.ignored_dirs:
                                continue
                            collect(entry.path, base_root)
                        elif entry.is_file():
                            if (name_lower.endswith('.tmp')
                                    or name_lower.startswith('._')
                                    or name_lower in cfg.ignored_files
                                    or 'page-preview' in name_lower):
                                continue
                            rel = os.path.normpath(os.path.relpath(entry.path, base_root))
                            if cfg.is_ignored(rel):
                                continue
                            rels.add(rel)
            except FileNotFoundError:
                pass

        collect(cfg.local_vault, cfg.local_vault)
        collect(cfg.icloud_vault, cfg.icloud_vault)
        collect(cfg.history_dir, cfg.history_dir)
        return rels

    # ── Per-file sync logic ──────────────────────────────────────

    async def sync_file(self, rel_path):
        cfg = self.config
        now = time.time()
        if rel_path in self.cooldowns and self.cooldowns[rel_path] > now:
            return

        local = os.path.join(cfg.local_vault, rel_path)
        icloud = os.path.join(cfg.icloud_vault, rel_path)
        history = os.path.join(cfg.history_dir, rel_path)
        d = cfg.disp(rel_path)

        L_exists = safe_exists(local)
        C_exists = safe_exists(icloud)
        H_exists = safe_exists(history)

        # ── Nothing exists anywhere ──
        if not L_exists and not C_exists:
            if H_exists:
                self.log.warn("REMOVING HISTORY",
                              f"{colored('No local', Fore.RED)} & {colored('No iCloud', Fore.RED)} for {d}",
                              level="important")
                await self.io.remove_file(history, "history")
            # Clean stale state
            if rel_path in self.hasher.state:
                del self.hasher.state[rel_path]
                self.hasher.dirty = True
            return

        # Helper: re-hash after stability window
        async def recheck():
            await asyncio.sleep(cfg.stability_window)
            Lh = await self.hasher.get_cached_hash(local, 'L', rel_path, force=True) if safe_exists(local) else None
            Ch = await self.hasher.get_cached_hash(icloud, 'C', rel_path, force=True) if safe_exists(icloud) else None
            Hh = await self.hasher.get_cached_hash(history, 'H', rel_path, force=True) if safe_exists(history) else None
            return Lh, Ch, Hh

        # ── Local missing, C+H exist ──
        if not L_exists and C_exists and H_exists:
            self.log.warn("DELETE",
                          f"{colored('Local missing', Fore.RED)}, stabilizing for {d}",
                          level="verbose")
            Lh, Ch, Hh = await recheck()
            if Ch is not None and Hh is not None and Ch == Hh:
                self.log.custom([" ←", "⚫"], [Fore.RED, Fore.RED], "DELETE",
                                f"{colored('Removing from iCloud', Fore.CYAN)} & history for {d}",
                                rel_path, level="verbose")
                await self.io.remove_file(icloud, "iCloud")
                await self.io.remove_file(history, "history")
            else:
                self.log.custom([" ↓", "⚪"], [Fore.CYAN, Fore.CYAN], "PULL",
                                f"{colored('Restoring to local', Fore.GREEN)} from iCloud for {d}",
                                rel_path, level="verbose")
                await self.restore_from_icloud(rel_path)
            return

        # ── iCloud missing, L+H exist ──
        if not C_exists and L_exists and H_exists:
            self.log.warn("DELETE",
                          f"{colored('iCloud missing', Fore.RED)}, stabilizing for {d}",
                          level="verbose")
            Lh, Ch, Hh = await recheck()
            if Lh is not None and Hh is not None and Lh == Hh:
                self.log.custom([" ←", "⚫"], [Fore.RED, Fore.RED], "DELETE",
                                f"{colored('Removing local', Fore.RED)} & history for {d}",
                                rel_path, level="verbose")
                await self.io.remove_file(local, "local")
                await self.io.remove_file(history, "history")
            else:
                self.log.custom([" ↑", "⚪"], [Fore.GREEN, Fore.GREEN], "PUSH",
                                f"Local changed for {d} -> pushing to iCloud",
                                rel_path, level="verbose")
                await self.push_to_icloud(rel_path)
            return

        # ── New local file (L only) ──
        if L_exists and not C_exists and not H_exists:
            self.log.custom([" →", "⚪"], [Fore.LIGHTBLACK_EX, Fore.GREEN], "NEW",
                            f"{colored('Local-only', Fore.GREEN)}, stabilizing {d}",
                            rel_path, level="verbose")
            Lh, Ch, Hh = await recheck()
            if Lh is None:
                self.log.info("SKIP", f"After stabilize local missing for {d}", level="verbose")
                return
            if size_or_zero(local) < cfg.tiny_threshold:
                self.log.info("SKIP", f"Local too small, deferring {d}", level="verbose")
                return
            self.log.custom([" ↑", "⚪"], [Fore.GREEN, Fore.GREEN], "PUSH",
                            f"{colored('Pushing to iCloud', Fore.CYAN)} for {d}",
                            rel_path, level="verbose")
            await self.push_to_icloud(rel_path)
            return

        # ── New iCloud file (C only) ──
        if C_exists and not L_exists and not H_exists:
            self.log.custom([" →", "⚪"], [Fore.LIGHTBLACK_EX, Fore.BLUE], "NEW",
                            f"{colored('iCloud-only', Fore.CYAN)}, stabilizing {d}",
                            rel_path, level="verbose")
            Lh, Ch, Hh = await recheck()
            if Ch is None:
                self.log.info("SKIP", f"After stabilize iCloud missing for {d}", level="verbose")
                return
            if size_or_zero(icloud) < cfg.tiny_threshold:
                self.log.info("SKIP", f"iCloud too small, deferring {d}", level="verbose")
                return
            self.log.custom([" ↓", "⚪"], [Fore.CYAN, Fore.CYAN], "PULL",
                            f"{colored('Restoring to local', Fore.GREEN)} for {d}",
                            rel_path, level="verbose")
            await self.restore_from_icloud(rel_path)
            return

        # ── Both sides exist or mixed states ──
        ensure_dir(os.path.dirname(history))

        L = await self.hasher.get_cached_hash(local, 'L', rel_path) if safe_exists(local) else None
        C = await self.hasher.get_cached_hash(icloud, 'C', rel_path) if safe_exists(icloud) else None
        H = await self.hasher.get_cached_hash(history, 'H', rel_path) if safe_exists(history) else None

        # History missing — seed it
        if H is None and (L is not None or C is not None):
            self.log.info("HISTORY MISSING", f"Seeding history for {d}")
            Lh, Ch, Hh = await recheck()

            if Lh is not None and Ch is not None:
                if Lh == Ch:
                    await self.io.async_copy(local, history)
                    H = Lh
                    self.log.info("HISTORY", f"Initialized history (identical) for {d}", level="verbose")
                else:
                    # Conflict at start with no history — newer wins
                    self.log.warn("HISTORY", f"Local and iCloud differ for {d}!", level="verbose")
                    local_m, icloud_m = safe_mtime(local), safe_mtime(icloud)
                    if local_m >= icloud_m:
                        self.log.warn("CONFLICT",
                                      f"{colored('Local is newer', Fore.YELLOW)}: {d}",
                                      level="important")
                        await self.io.create_conflict_duplicate(icloud)
                        await self.push_to_icloud(rel_path)
                    else:
                        self.log.warn("CONFLICT",
                                      f"{colored('iCloud is newer', Fore.YELLOW)}: {d}",
                                      level="important")
                        await self.io.create_conflict_duplicate(local)
                        await self.restore_from_icloud(rel_path)
                    return
            elif Lh is not None and size_or_zero(local) >= cfg.min_seed_size(rel_path):
                await self.io.async_copy(local, history)
                H = Lh
                self.log.info("HISTORY",
                              f"Initialized {colored('from local', Fore.GREEN)} for {d}",
                              level="verbose")
            elif Ch is not None and size_or_zero(icloud) >= cfg.min_seed_size(rel_path):
                await self.io.async_copy(icloud, history)
                H = Ch
                self.log.info("HISTORY",
                              f"Initialized {colored('from iCloud', Fore.CYAN)} for {d}",
                              level="verbose")
            else:
                if size_or_zero(local) > 0 or size_or_zero(icloud) > 0:
                    self.log.error("FAILED", f"Files unreadable for {d}; retrying next pass")
                else:
                    self.log.info("SKIP", f"History seeding skipped for {d}", level="verbose")
                return

        # CASE A: Identical
        if L == C == H:
            return

        # CASE B: Local changed
        if L is not None and H is not None and L != H and C == H:
            self.log.custom([" ↑", "⚪"], [Fore.GREEN, Fore.GREEN], "PUSH",
                            f"{colored('Local changed', Fore.GREEN)}, pushing for {d}",
                            rel_path, level="verbose")
            await self.push_to_icloud(rel_path)
            return

        # CASE C: iCloud changed
        if C is not None and H is not None and C != H and L == H:
            self.log.custom([" ↓", "⚪"], [Fore.CYAN, Fore.CYAN], "PULL",
                            f"{colored('iCloud changed', Fore.CYAN)}, restoring for {d}",
                            rel_path, level="verbose")
            await self.restore_from_icloud(rel_path)
            return

        # CASE D: Both changed (rare)
        self.log.warn("CONFLICT",
                      f"Both changed, stabilizing {d} {cfg.stabilize_wait}s",
                      level="important")
        await asyncio.sleep(cfg.stabilize_wait)

        L2 = await self.hasher.get_cached_hash(local, 'L', rel_path, force=True) if safe_exists(local) else None
        C2 = await self.hasher.get_cached_hash(icloud, 'C', rel_path, force=True) if safe_exists(icloud) else None

        if L2 is not None and L2 != L:
            self.log.warn("CONFLICT",
                          f"{colored('Local still changing', Fore.YELLOW)}, choose local: {d}",
                          level="important")
            await self.io.create_conflict_duplicate(icloud)
            await self.push_to_icloud(rel_path)
            return

        if C2 is not None and C2 != C:
            self.log.warn("CONFLICT",
                          f"{colored('iCloud still changing', Fore.YELLOW)}, choose iCloud: {d}",
                          level="important")
            await self.io.create_conflict_duplicate(local)
            await self.restore_from_icloud(rel_path)
            return

        if not safe_exists(local):
            self.log.custom([" ↓", "🟡"], [Fore.CYAN, Fore.YELLOW], "PULL",
                            f"{colored('Local vanished', Fore.YELLOW)}, restoring from iCloud: {d}",
                            rel_path, level="verbose")
            await self.restore_from_icloud(rel_path)
            return

        if not safe_exists(icloud):
            self.log.custom([" ↑", "🟡"], [Fore.GREEN, Fore.YELLOW], "PUSH",
                            f"{colored('iCloud vanished', Fore.YELLOW)}, pushing local: {d}",
                            rel_path, level="verbose")
            await self.push_to_icloud(rel_path)
            return

        # Fallback: mtime comparison
        local_m, icloud_m = safe_mtime(local), safe_mtime(icloud)
        if local_m >= icloud_m:
            self.log.info("CONFLICT",
                          f"{colored('Local is newer', Fore.YELLOW)}, push local: {d}",
                          level="important")
            await self.io.create_conflict_duplicate(icloud)
            await self.push_to_icloud(rel_path)
        else:
            self.log.info("CONFLICT",
                          f"{colored('iCloud is newer', Fore.YELLOW)}, pull iCloud: {d}",
                          level="important")
            await self.io.create_conflict_duplicate(local)
            await self.restore_from_icloud(rel_path)

    # ── Concurrency wrapper ──────────────────────────────────────

    async def _sync_wrapper(self, rel):
        try:
            async with self.io_semaphore:
                await self.sync_file(rel)
        except Exception as e:
            self.log.error("ERROR", f"Error syncing {self.config.disp(rel)}: {e}")
            self.log.error("TRACEBACK", traceback.format_exc())
        finally:
            self.active_tasks.discard(rel)

    # ── Main loop ────────────────────────────────────────────────

    async def run(self):
        cfg = self.config

        # Validate config
        errors = cfg.validate()
        for level, msg in errors:
            if level == "critical":
                self.log.error("CONFIG", msg, level="important")
                sys.exit(1)
            else:
                self.log.warn("CONFIG", msg, level="important")

        await self.log.cleanup_old_logs()
        self.log.init_log_file()

        mode_str = "DAEMON MODE" if cfg.run_continuously else "ONE-SHOT MODE"
        self.log.startup(mode_str)
        self.duplicates.scan_and_clean()
        self.hasher.load_state()
        last_save = time.time()

        try:
            while True:
                try:
                    # Cleanup expired cooldowns
                    now = time.time()
                    expired = [k for k, v in self.cooldowns.items() if v <= now]
                    for k in expired:
                        del self.cooldowns[k]

                    # Gather files
                    rel_paths = self.gather_rel_paths()
                    # Include files tracked in state (for deletion detection)
                    rel_paths.update(k for k, v in self.hasher.state.items() if v)

                    # Launch tasks for files not already being processed
                    active_rels = [r for r in sorted(rel_paths) if r not in self.active_tasks]
                    tasks = []

                    if active_rels and not cfg.run_continuously:
                        self.log.header(len(active_rels))

                    for rel in active_rels:
                        self.active_tasks.add(rel)
                        tasks.append(asyncio.create_task(self._sync_wrapper(rel)))

                    # One-shot mode: wait and exit
                    if not cfg.run_continuously:
                        if tasks:
                            self.log.info("INFO",
                                          f"One-shot: waiting for {len(tasks)} tasks...",
                                          level="normal")
                            await asyncio.gather(*tasks)
                        else:
                            self.log.info("INFO", "Nothing to sync.", level="normal")
                        self.hasher.save_state()
                        self.log.flush()
                        self.log.success("DONE", "One-shot sync complete.", level="normal")
                        break

                    # Daemon mode: idle indicator
                    if not tasks:
                        self.log.idle()

                    # Periodic state save (every 5s)
                    now_t = time.time()
                    if self.hasher.dirty and now_t - last_save > 5:
                        self.hasher.save_state()
                        self.log.flush()
                        last_save = now_t
                    elif now_t - last_save > 5:
                        self.log.flush()
                        last_save = now_t

                except Exception as outer:
                    self.log.error("ERROR", f"Unexpected error in main loop: {outer}")
                    self.log.error("TRACEBACK", traceback.format_exc())

                await asyncio.sleep(cfg.poll_interval)

        except (KeyboardInterrupt, asyncio.CancelledError):
            self.log.warn("INFO", "Shutdown requested, saving state...", level="important")
            self.hasher.save_state()
            self.log.flush()
            self.log.success("DONE", "Graceful shutdown complete.", level="important")
            sys.exit(0)
