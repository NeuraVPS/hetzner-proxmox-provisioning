#!/usr/bin/env python3
"""
Replace C:\\ProgramData\\NeuraVPS\\mt_hook_launcher.vbs on every running
Windows VM that already has it. VMs missing the file are left alone (the
hook is only deployed by recent provisioning, and we do not want to
install it on VMs that never had it).

Usage on a base node:
  python3 update_mt_hook_launcher.py [--only 100,101] [--skip 102]
                                     [--gte 200] [--lte 300] [--dry-run]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import smbclient

from smb_fleet import (
    TaskResult,
    VMTarget,
    add_filter_args,
    parsed_filters,
    run_on_fleet,
    smb_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_VBS = REPO_ROOT / "windows_vm" / "hooks" / "mt_hook_launcher.vbs"
REMOTE_REL = r"ProgramData\NeuraVPS\mt_hook_launcher.vbs"


def _build_task(payload: bytes):
    def task(target: VMTarget) -> TaskResult:
        path = smb_path(target, REMOTE_REL)
        try:
            smbclient.stat(path)
        except FileNotFoundError:
            return TaskResult(target.vmid, "skipped", "file not present")

        try:
            with smbclient.open_file(path, mode="rb") as f:
                current = f.read()
        except Exception as e:
            return TaskResult(target.vmid, "error", f"read failed: {e}")

        if current == payload:
            return TaskResult(target.vmid, "skipped", "already up-to-date")

        try:
            with smbclient.open_file(path, mode="wb") as f:
                f.write(payload)
        except Exception as e:
            return TaskResult(target.vmid, "error", f"write failed: {e}")

        return TaskResult(
            target.vmid, "ok", f"replaced ({len(current)} -> {len(payload)} bytes)"
        )

    return task


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_filter_args(parser)
    args = parser.parse_args()

    if not SOURCE_VBS.exists():
        raise SystemExit(f"source file not found: {SOURCE_VBS}")
    payload = SOURCE_VBS.read_bytes()

    run_on_fleet(_build_task(payload), **parsed_filters(args))


if __name__ == "__main__":
    main()
