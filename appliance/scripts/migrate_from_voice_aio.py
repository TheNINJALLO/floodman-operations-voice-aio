#!/usr/bin/env python3
from __future__ import annotations
import argparse, shutil
from pathlib import Path


def copy_if_missing(source: Path, destination: Path) -> None:
    if source.exists() and not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        print(f"migrated {source} -> {destination}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("old_data", type=Path)
    parser.add_argument("--new-data", type=Path, default=Path("/home/container/data"))
    args = parser.parse_args()
    copy_if_missing(args.old_data / "knowledge", args.new_data / "knowledge")
    copy_if_missing(args.old_data / "floodman-voice.sqlite3", args.new_data / "legacy-floodman-voice.sqlite3")
    copy_if_missing(args.old_data / "recordings", args.new_data / "recordings")
    print("Legacy databases are preserved for audit; new calls use floodman.db.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
