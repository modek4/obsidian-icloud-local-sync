import os
import re
import sys
import asyncio

from datetime import datetime
from colorama import Fore, Style, init as colorama_init

LEVEL_MAP = {"quiet": 0, "normal": 10, "verbose": 100, "important": 1000}
_ANSI_RE = re.compile(r'\x1b\[([0-9]{1,3}(;[0-9]{1,2})?)?[mGK]')

def strip_ansi(text: str) -> str:
    """
    Deletes ANSI escape sequences from a string.

    Args:
        text (str): The raw string containing ANSI codes.
    Returns:
        str: The plain text string suitable for writing to standard log files.
    """
    return _ANSI_RE.sub('', text)

def colored(text: str, color) -> str:
    """
    Wraps text in ANSI color codes for console formatting.

    Args:
        text (str): The string to format.
        color: The colorama foreground color code to apply.
    Returns:
        str: The color-formatted string ending with a reset sequence.
    """
    return color + text + Style.RESET_ALL

class SyncLogger:
    """
    Outputs colored status events to the console based on `LEVEL_MAP` and maintains an asynchronous-friendly buffer for writing plain-text logs to disk. It also handles automatic log cleanup based on configuration.
    """
    def __init__(self, config):
        """
        Initializes the SyncLogger.

        Args:
            config (SyncConfig): The application configuration instance.
        """
        self.config = config
        self.log_file: str | None = None
        self._buffer: list[str] = []
        colorama_init()

    def init_log_file(self):
        """
        Generates and assigns the log file path using the current timestamp. Should be called after verifying the logs directory exists.
        """
        ts = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        self.log_file = os.path.join(self.config.logs_dir, f"sync_{ts}.log")

    # ── Core output ──────────────────────────────────────────────

    def console_event(self, icon: str, color, msg_type: str, msg: str, level: str = "normal"):
        """
        Formats and prints a colored log event to the console.

        Args:
            icon (str): A short emoji or symbol representing the event.
            color: The colorama foreground color code.
            msg_type (str): A short categorical label (e.g., 'INFO', 'PUSH').
            msg (str): The main log message.
            level (str, optional): The `LEVEL_MAP`
        """
        ts = datetime.now().strftime('%H:%M:%S')
        message = f"  {ts:<4}" + color + f" {icon} {msg_type:<12}" + Style.RESET_ALL + f" {msg}"
        if level.lower() == "important":
            print(message)
            return
        if self.config.console_level.lower() == "quiet":
            return
        if LEVEL_MAP.get(self.config.console_level.lower(), 10) >= LEVEL_MAP.get(level.lower(), 10):
            print(message)

    def write_to_file(self, msg_type: str, msg: str):
        """
        Appends a stripped log message to the internal buffer. If the buffer size reaches the limit (20 entries), it automatically triggers a flush to write the contents to disk.

        Args:
            msg_type (str): The categorical label of the message.
            msg (str): The raw log message (ANSI codes will be stripped).
        """
        if not self.log_file:
            return
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._buffer.append(f"[{ts}] [{msg_type}] {strip_ansi(msg)}\n")
        if len(self._buffer) >= 20:
            self.flush()

    def flush(self):
        """
        Writes all buffered log messages to the log file and clears the buffer.
        """
        if not self._buffer or not self.log_file:
            return
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.writelines(self._buffer)
            self._buffer.clear()
        except Exception as e:
            self.console_event("🔴", Fore.RED, "FAILED", f"Log Write Failed: {e}", level="important")

    # ── Log methods ──────────────────────────────────────────────

    def info(self, msg_type: str, msg: str, level: str = "verbose"):
        """
        Logs an informational event (blue icon).

        Args:
            msg_type (str): The category of the message (e.g., 'INFO').
            msg (str): The main log message.
            level (str, optional): The `LEVEL_MAP`.
        """
        self.console_event("🔵", Fore.CYAN, msg_type, msg, level=level)
        self.write_to_file(msg_type, msg)

    def warn(self, msg_type: str, msg: str, level: str = "verbose"):
        """
        Logs a warning event (yellow icon).

        Args:
            msg_type (str): The category of the message (e.g., 'WARN').
            msg (str): The main log message.
            level (str, optional): The `LEVEL_MAP`.
        """
        self.console_event("🟡", Fore.YELLOW, msg_type, msg, level=level)
        self.write_to_file(msg_type, msg)

    def error(self, msg_type: str, msg: str, level: str = "important", critical: bool = False):
        """
        Logs an error event (red icon).

        Args:
            msg_type (str): The error category.
            msg (str): The error message.
            level (str, optional): The `LEVEL_MAP`.
            critical (bool, optional): If True, flushes logs and forcibly exits the program via `sys.exit(1)` to prevent data corruption. Defaults to False.
        """
        self.console_event("🔴", Fore.RED, msg_type, msg, level=level)
        self.write_to_file(msg_type, msg)
        if critical:
            self.write_to_file("ERROR", "Critical error. Stopping to prevent data loss.")
            self.flush()
            self.console_event("🔴", Fore.RED, "ERROR", "Critical error. Stopping to prevent data loss.", level="important")
            sys.exit(1)

    def success(self, msg_type: str, msg: str, level: str = "important"):
        """
        Logs a success event (green icon).

        Args:
            msg_type (str): The category of the message (e.g., 'SUCCESS').
            msg (str): The main log message.
            level (str, optional): The `LEVEL_MAP`.
        """
        self.console_event("🟢", Fore.GREEN, msg_type, msg, level=level)
        self.write_to_file(msg_type, msg)

    def custom(self, icons: list[str], colors: list, msg_type: str, msg: str, rel_path: str, level: str = "normal") -> None:
        """
        Logs a custom formatted event that changes output based on verbosity.

        Args:
            icons (list[str]): A list of two icons: `[normal_icon, verbose_icon]`.
            colors (list): A list of two colorama codes: `[normal_color, verbose_color]`.
            msg_type (str): The categorical label.
            msg (str): The full detailed message for verbose mode.
            rel_path (str): The shortened path to display in normal mode.
            level (str, optional): The `LEVEL_MAP`.
        """
        if self.config.console_level.lower() != "verbose":
            self.console_event(icons[0], colors[0], msg_type, rel_path, level="normal")
        else:
            self.console_event(icons[1], colors[1], msg_type, msg, level=level)
        self.write_to_file(msg_type, msg)

    # ── Console helpers ──────────────────────────────────────────

    def header(self, files_checked: int):
        """
        Prints a scanning header for runs.

        Args:
            files_checked (int): The number of files that will be scanned, used for display purposes
        """
        ts = datetime.now().strftime('%H:%M:%S')
        print(f"  {ts:<4}" + Fore.CYAN + f" 🔵 {'INFO':<12} Scanning... {files_checked} files" + Style.RESET_ALL, flush=True)

    def idle(self):
        """
        Prints a transient idle indicator.
        """
        if self.config.console_level.lower() == "quiet":
            return
        ts = datetime.now().strftime('%H:%M:%S')
        print(f"  {ts:<4}" + Fore.CYAN + f" 🔵 {'INFO':<12} No changes." + Style.RESET_ALL, end="\r", flush=True)

    def startup(self, mode_str: str):
        """
        Prints the application startup banner and configuration summary.

        Args:
            mode_str (str): The operation mode label (e.g., 'DAEMON MODE').
        """
        log_path = self.log_file or "(not initialized)"
        print(Fore.WHITE + Style.BRIGHT + "=" * 75 + Style.RESET_ALL)
        print(Fore.CYAN + Style.BRIGHT + "  Obsidian Sync" + Style.RESET_ALL)
        print(Fore.WHITE + f"  Mode:   {mode_str}" + Style.RESET_ALL)
        print(Fore.WHITE + f"  Local:  {self.config.local_vault}" + Style.RESET_ALL)
        print(Fore.WHITE + f"  iCloud: {self.config.icloud_vault}" + Style.RESET_ALL)
        print(Fore.WHITE + f"  Log:    {log_path}" + Style.RESET_ALL)
        print(Fore.WHITE + Style.BRIGHT + "=" * 75 + Style.RESET_ALL)

    def list_log_files(self, logs_dir: str) -> list[str]:
        """
        Retrieves a chronologically sorted list of log files.

        Args:
            logs_dir (str): The directory containing log files.
        Returns:
            list[str]: A list of absolute paths to log files, sorted oldest to newest.
        """
        try:
            return sorted([os.path.join(logs_dir, f) for f in os.listdir(logs_dir) if f.endswith('.log')], key=os.path.getmtime)
        except FileNotFoundError:
            return []

    async def cleanup_old_logs(self):
        """
        Asynchronously deletes older log files to maintain the retention limit. Hard minimum limit of keeping at least 1 log file.
        """
        try:
            logs_dir = self.config.logs_dir
            logs = await asyncio.to_thread(self.list_log_files, logs_dir)
            keep = max(1, self.config.log_retention)
            if len(logs) <= keep:
                return
            for old in logs[:-keep]:
                await asyncio.to_thread(os.remove, old)
        except Exception as e:
            self.console_event("🔴", Fore.RED, "FAILED", f"Log Cleanup Failed: {e}", level="important")
