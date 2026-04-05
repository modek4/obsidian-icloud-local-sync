import os
import re
import asyncio
from datetime import datetime

from colorama import Fore, Style, init as colorama_init

LEVEL_MAP = {"quiet": 0, "normal": 10, "verbose": 100, "important": 1000}
_ANSI_RE = re.compile(r'\x1b\[([0-9]{1,3}(;[0-9]{1,2})?)?[mGK]')


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


def colored(text: str, color) -> str:
    return color + text + Style.RESET_ALL


class SyncLogger:
    def __init__(self, config):
        self.config = config
        self.log_file: str | None = None
        self._buffer: list[str] = []
        colorama_init()

    def init_log_file(self):
        ts = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        self.log_file = os.path.join(self.config.logs_dir, f"sync_{ts}.log")

    # ── Core output ──────────────────────────────────────────────

    def console_event(self, icon, color, msg_type, msg, level="normal"):
        ts = datetime.now().strftime('%H:%M:%S')
        message = f"  {ts:<4}" + color + f" {icon} {msg_type:<12}" + Style.RESET_ALL + f" {msg}"
        if level.lower() == "important":
            print(message)
            return
        if self.config.console_level.lower() == "quiet":
            return
        if LEVEL_MAP.get(self.config.console_level.lower(), 10) >= LEVEL_MAP.get(level.lower(), 10):
            print(message)

    def _write_to_file(self, msg_type, msg):
        if not self.log_file:
            return
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._buffer.append(f"[{ts}] [{msg_type}] {strip_ansi(msg)}\n")
        if len(self._buffer) >= 20:
            self.flush()

    def flush(self):
        if not self._buffer or not self.log_file:
            return
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.writelines(self._buffer)
        except Exception as e:
            self.console_event("🔴", Fore.RED, "FAILED", f"Log Write Failed: {e}", level="important")
        self._buffer.clear()

    # ── Log methods ──────────────────────────────────────────────

    def info(self, msg_type, msg, level="verbose"):
        self.console_event("🔵", Fore.CYAN, msg_type, msg, level=level)
        self._write_to_file(msg_type, msg)

    def warn(self, msg_type, msg, level="verbose"):
        self.console_event("🟡", Fore.YELLOW, msg_type, msg, level=level)
        self._write_to_file(msg_type, msg)

    def error(self, msg_type, msg, level="important", critical=False):
        self.console_event("🔴", Fore.RED, msg_type, msg, level=level)
        self._write_to_file(msg_type, msg)
        if critical:
            import sys
            self.console_event("🔴", Fore.RED, "ERROR",
                               "Critical error. Stopping to prevent data loss.", level="important")
            sys.exit(1)

    def success(self, msg_type, msg, level="important"):
        self.console_event("🟢", Fore.GREEN, msg_type, msg, level=level)
        self._write_to_file(msg_type, msg)

    def custom(self, icons, colors, msg_type, msg, rel_path, level="normal"):
        if self.config.console_level.lower() != "verbose":
            self.console_event(icons[0], colors[0], msg_type, rel_path, level="normal")
        else:
            self.console_event(icons[1], colors[1], msg_type, msg, level=level)
        self._write_to_file(msg_type, msg)

    # ── Console helpers ──────────────────────────────────────────

    def header(self, files_checked: int):
        ts = datetime.now().strftime('%H:%M:%S')
        print(f"  {ts:<4}" + Fore.CYAN + f" 🔵 {'INFO':<12} Scanning... {files_checked} files"
              + Style.RESET_ALL, flush=True)

    def idle(self):
        if self.config.console_level.lower() == "quiet":
            return
        ts = datetime.now().strftime('%H:%M:%S')
        print(f"  {ts:<4}" + Fore.CYAN + f" 🔵 {'INFO':<12} No changes."
              + Style.RESET_ALL, end="\r", flush=True)

    def startup(self, mode_str: str):
        print(Fore.WHITE + Style.BRIGHT + "=" * 75 + Style.RESET_ALL)
        print(Fore.CYAN + Style.BRIGHT + "  Obsidian Sync" + Style.RESET_ALL)
        print(Fore.WHITE + f"  Mode:   {mode_str}" + Style.RESET_ALL)
        print(Fore.WHITE + f"  Local:  {self.config.local_vault}" + Style.RESET_ALL)
        print(Fore.WHITE + f"  iCloud: {self.config.icloud_vault}" + Style.RESET_ALL)
        print(Fore.WHITE + f"  Log:    {self.log_file}" + Style.RESET_ALL)
        print(Fore.WHITE + Style.BRIGHT + "=" * 75 + Style.RESET_ALL)

    async def cleanup_old_logs(self):
        try:
            logs_dir = self.config.logs_dir
            if not os.path.exists(logs_dir):
                return
            logs = await asyncio.to_thread(
                lambda: [os.path.join(logs_dir, f) for f in os.listdir(logs_dir) if f.endswith('.log')]
            )
            keep = self.config.log_retention
            if len(logs) <= keep:
                return
            logs.sort(key=os.path.getmtime)
            for old in logs[:-keep]:
                await asyncio.to_thread(os.remove, old)
        except Exception as e:
            self.console_event("🔴", Fore.RED, "FAILED", f"Log Cleanup Failed: {e}", level="important")
