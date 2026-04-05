import os
import re
import sys


class DuplicateScanner:
    def __init__(self, config, logger, disk_io):
        self.config = config
        self.log = logger
        self.io = disk_io

    def scan_and_clean(self):
        conflict_re = re.compile(r'_CONFLICT_\d{8}_\d{6}_\d{6}')
        icloud_dup_re = re.compile(r'\s\(\d+\)\.[^.]+$')
        tmp_re = re.compile(r'\.tmp$')

        duplicates = []
        self.log.info("INFO", "Scanning for conflict/duplicate files...", level="important")

        for root_dir in [self.config.local_vault, self.config.icloud_vault, self.config.history_dir]:
            if not os.path.exists(root_dir):
                continue
            for dirpath, _, filenames in os.walk(root_dir):
                if '.trash' in dirpath.lower().split(os.sep):
                    continue
                for f in filenames:
                    if conflict_re.search(f) or icloud_dup_re.search(f) or tmp_re.search(f):
                        duplicates.append(os.path.join(dirpath, f))

        if not duplicates:
            self.log.success("CLEAN", "No duplicates found.", level="important")
            return

        self.log.error("DANGER",
                       f"Found {len(duplicates)} potential conflict/duplicate files.",
                       level="important")
        for p in duplicates:
            self.log.warn("DUPLICATE", self.config.disp(p), level="important")

        self.log.warn("ACTION",
                      "Delete them WITHOUT RECOVERY before sync? (Y/n)",
                      level="important")
        sys.stdout.flush()
        ans = input().strip().lower()

        if ans in ('y', 'yes'):
            for p in duplicates:
                self.io.remove_file_sync(p, "Duplicate/Conflict")
            self.log.success("CLEAN", "All duplicates removed.", level="important")
        else:
            self.log.warn("INFO", "Skipping duplicate cleanup.", level="important")
        print("-" * 75)
