import asyncio
import argparse
import os
import sys

from .config import SyncConfig
from .logger import SyncLogger
from .hasher import FileHasher
from .disk_io import DiskIO
from .duplicates import DuplicateScanner
from .sync_engine import SyncEngine

def main():
    """
    Entry point for the Obsidian iCloud Windows Sync.
    """
    parser = argparse.ArgumentParser(description="Obsidian iCloud Windows Sync")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config YAML file (default: config.yaml)")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    try:
        config = SyncConfig.from_yaml(args.config)
    except FileNotFoundError:
        print(f"Config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)
    except PermissionError:
        print(f"Permission denied while reading config file: {args.config}", file=sys.stderr)
        sys.exit(1)
    except IsADirectoryError:
        print(f"Config path is a directory, not a file: {args.config}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Invalid config in {args.config}: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Unable to read config file {args.config}: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error while loading config from {args.config}: {e}", file=sys.stderr)
        sys.exit(1)

    logger = SyncLogger(config)
    hasher = FileHasher(config, logger)
    disk_io = DiskIO(config, logger)
    duplicates = DuplicateScanner(config, logger, disk_io)
    duplicates.scan_and_clean()
    engine = SyncEngine(config, logger, hasher, disk_io, duplicates)

    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
