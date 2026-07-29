#!/usr/bin/env python3
"""
Replace one of the C:\\ProgramData\\NeuraVPS\\*_hook_launcher.vbs files on every
running Windows VM that already has it. VMs missing the file are left alone:
the hook only belongs where provisioning put it, and this must never install
one on a VM that never had it.

Usage on a base node:
  python3 update_mt_hook_launcher.py [--launcher mt_hook_launcher.vbs]
                                     [--only 100,101] [--skip 102]
                                     [--gte 200] [--lte 300] [--dry-run]

--launcher takes any of mt_hook_launcher.vbs (default), sqx_hook_launcher.vbs
or sqx144_hook_launcher.vbs, so all three share this one proven path instead
of a copy per launcher. The source file is read from the current directory.
"""

from __future__ import annotations

import argparse
import errno
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

_LAUNCHERS = ("mt_hook_launcher.vbs", "sqx_hook_launcher.vbs",
              "sqx144_hook_launcher.vbs")


def _build_task(payload: bytes, remote_rel: str):
    def task(target: VMTarget) -> TaskResult:
        path = smb_path(target, remote_rel)
        try:
            smbclient.stat(path)
        except OSError as e:
            # smbprotocol raises SMBOSError (sibling of FileNotFoundError) for
            # both "file missing" (NtStatus 0xc0000034) and "parent folder
            # missing" (0xc000003a). Both surface as errno=ENOENT.
            if e.errno == errno.ENOENT:
                return TaskResult(target.vmid, "skipped", "file not present")
            return TaskResult(target.vmid, "error", f"stat failed: {e}")

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
    parser.add_argument("--launcher", default=_LAUNCHERS[0], choices=_LAUNCHERS,
                        help="which launcher to replace (default: %(default)s)")
    add_filter_args(parser)
    args = parser.parse_args()

    source = Path(args.launcher)
    if not source.exists():
        raise SystemExit(f"source file not found: {source}")
    remote_rel = rf"ProgramData\NeuraVPS\{args.launcher}"
    payload = source.read_bytes()

    run_on_fleet(_build_task(payload, remote_rel), **parsed_filters(args))


if __name__ == "__main__":
    main()
