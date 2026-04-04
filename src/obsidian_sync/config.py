import os
import sys
import fnmatch
from dataclasses import dataclass, field

import yaml


@dataclass
class SyncConfig:
    # Paths
    local_vault: str = ""
    icloud_vault: str = ""
    history_dir: str = ""
    logs_dir: str = ""

    # Sync
    run_continuously: bool = True
    poll_interval: int = 2
    stability_window: int = 3
    stabilize_wait: int = 8
    cooldown_seconds: int = 3
    big_file_cooldown: int = 30
    big_file_threshold: int = 100 * 1024
    tiny_threshold: int = 8

    # Logging
    console_level: str = "normal"
    shorter_paths: bool = True
    max_display_length: int = 50
    log_retention: int = 10

    # Ignore
    ignore_patterns: list = field(default_factory=list)
    ignored_dirs: set = field(default_factory=lambda: {
        '.trash', '.fseventsd', '.spotlight-v100', '.apdisk'
    })
    ignored_files: set = field(default_factory=lambda: {
        '.ds_store', '.trash', 'workspace.json', 'workspace-mobile.json'
    })

    @classmethod
    def from_yaml(cls, path: str) -> "SyncConfig":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        paths = data.get("paths", {})
        sync = data.get("sync", {})
        logging_cfg = data.get("logging", {})
        ignore = data.get("ignore", {})

        return cls(
            local_vault=paths.get("local_vault", ""),
            icloud_vault=paths.get("icloud_vault", ""),
            history_dir=paths.get("history_dir", ""),
            logs_dir=paths.get("logs_dir", ""),
            run_continuously=sync.get("run_continuously", True),
            poll_interval=sync.get("poll_interval", 2),
            stability_window=sync.get("stability_window", 3),
            stabilize_wait=sync.get("stabilize_wait", 8),
            cooldown_seconds=sync.get("cooldown_seconds", 3),
            big_file_cooldown=sync.get("big_file_cooldown", 30),
            big_file_threshold=sync.get("big_file_threshold", 100 * 1024),
            tiny_threshold=sync.get("tiny_threshold", 8),
            console_level=logging_cfg.get("console_level", "normal"),
            shorter_paths=logging_cfg.get("shorter_paths", True),
            max_display_length=logging_cfg.get("max_display_length", 50),
            log_retention=logging_cfg.get("log_retention", 10),
            ignore_patterns=ignore.get("patterns", []),
            ignored_dirs=set(ignore.get("dirs", [
                '.trash', '.fseventsd', '.spotlight-v100', '.apdisk'
            ])),
            ignored_files=set(ignore.get("files", [
                '.ds_store', '.trash', 'workspace.json', 'workspace-mobile.json'
            ])),
        )

    @property
    def state_file_path(self) -> str:
        return os.path.join(self.logs_dir, "sync_state.json")

    def validate(self) -> list[tuple[str, str]]:
        """Returns list of (level, message). level is 'critical' or 'warn'."""
        errors: list[tuple[str, str]] = []

        for name, path in [("local_vault", self.local_vault),
                           ("icloud_vault", self.icloud_vault)]:
            if not path:
                errors.append(("critical", f"{name} is not set"))
            elif not os.path.exists(path):
                errors.append(("critical", f"{name} does not exist: {path}"))

        for name, path in [("history_dir", self.history_dir),
                           ("logs_dir", self.logs_dir)]:
            if not path:
                errors.append(("critical", f"{name} is not set"))
            elif not os.path.exists(path):
                os.makedirs(path, exist_ok=True)
                errors.append(("warn", f"{name} did not exist, created: {path}"))

        if self.local_vault and self.icloud_vault:
            if os.path.normcase(self.local_vault) == os.path.normcase(self.icloud_vault):
                errors.append(("critical", "local_vault and icloud_vault cannot be the same path"))

        if self.history_dir:
            for name, guarded in [("local_vault", self.local_vault),
                                  ("icloud_vault", self.icloud_vault)]:
                if guarded and os.path.normcase(self.history_dir).startswith(
                        os.path.normcase(guarded) + os.sep):
                    errors.append(("critical", f"history_dir cannot be inside {name}"))

        for p in self.ignore_patterns:
            if '//' in p or p.startswith('/'):
                errors.append(("warn", f"Suspicious ignore pattern: '{p}'"))

        return errors

    def is_ignored(self, rel_path: str) -> bool:
        rel = rel_path.replace(os.sep, '/').lower()
        for pattern in self.ignore_patterns:
            pat = pattern.replace(os.sep, '/').lower()
            if rel == pat:
                return True
            if fnmatch.fnmatch(rel, pat):
                return True
            if rel.startswith(pat.rstrip('/') + '/'):
                return True
        return False

    def disp(self, path: str) -> str:
        """Shortened display path for logs."""
        if not self.shorter_paths:
            return path
        if os.path.isabs(path):
            for root in [self.local_vault, self.icloud_vault, self.history_dir]:
                if root and path.startswith(root):
                    path = os.path.relpath(path, root)
                    break
            else:
                return os.path.basename(path)
        if len(path) <= self.max_display_length:
            return path
        parts = path.split(os.sep)
        if len(parts) > 2:
            return f"{parts[0]}{os.sep}...{os.sep}{parts[-1]}"
        return f"...{os.sep}{os.path.basename(path)}"

    def min_seed_size(self, rel_path: str) -> int:
        return 1 if '.obsidian' in rel_path.lower() else self.tiny_threshold
