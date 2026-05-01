#!/usr/bin/env python3
"""
smb_fleet: Run a per-VM task against every running Windows VM via SMB.

Designed to run on a Proxmox base node that has:
  - Firebase credentials at /etc/firebase-credentials.json (or
    $FIREBASE_CREDENTIALS_FILE)
  - IPv6 reachability + port 445 to every VM (base nodes always do, even
    when a VM has Samba disabled at the user level)
  - The `smbprotocol` package installed (`pip install smbprotocol`)

Per-VM credentials are read from Firestore:
  servers/{id}.proxmoxId      (VMID)
  servers/{id}.ipv6           (target IPv6)
  servers/{id}.serverUser     (SMB username, mirrors the doc id)
  servers/{id}.serverPassword (SMB password)

Concrete tasks (one-shot or recurring) live in their own scripts; this
module just provides the fleet-iteration plumbing. See
update_mt_hook_launcher.py for an example.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import FieldFilter

import smbclient
from smbprotocol.exceptions import SMBException


@dataclass
class VMTarget:
    firestore_id: str
    vmid: int
    node_id: str
    ipv6: str
    username: str
    password: str
    raw: dict = field(default_factory=dict)


@dataclass
class TaskResult:
    vmid: int
    status: str   # "ok" | "skipped" | "error" | "unreachable"
    message: str = ""


TaskFn = Callable[[VMTarget], TaskResult]


def _init_firestore():
    if not firebase_admin._apps:
        creds_file = os.environ.get(
            "FIREBASE_CREDENTIALS_FILE", "/etc/firebase-credentials.json"
        )
        firebase_admin.initialize_app(credentials.Certificate(creds_file))
    return firestore.client()


def _list_running_targets(db, logger: logging.Logger) -> List[VMTarget]:
    docs = (
        db.collection("servers")
        .where(filter=FieldFilter("status", "==", "running"))
        .stream()
    )
    targets: List[VMTarget] = []
    incomplete = 0
    for doc in docs:
        d = doc.to_dict() or {}
        vmid = d.get("proxmoxId")
        ipv6 = d.get("ipv6")
        username = d.get("serverUser")
        password = d.get("serverPassword")
        if not (vmid and ipv6 and username and password):
            incomplete += 1
            continue
        targets.append(
            VMTarget(
                firestore_id=doc.id,
                vmid=int(vmid),
                node_id=d.get("nodeId", "") or "",
                ipv6=str(ipv6),
                username=str(username),
                password=str(password),
                raw=d,
            )
        )
    if incomplete:
        logger.warning(
            "skipped %d running servers with missing proxmoxId/ipv6/serverUser/serverPassword",
            incomplete,
        )
    return targets


def _apply_filters(
    targets: List[VMTarget],
    only_vmids: Optional[List[int]],
    skip_vmids: Optional[List[int]],
    vmid_gte: Optional[int],
    vmid_lte: Optional[int],
) -> List[VMTarget]:
    only_set = set(only_vmids) if only_vmids else None
    skip_set = set(skip_vmids) if skip_vmids else set()
    out = []
    for t in targets:
        if only_set is not None and t.vmid not in only_set:
            continue
        if t.vmid in skip_set:
            continue
        if vmid_gte is not None and t.vmid < vmid_gte:
            continue
        if vmid_lte is not None and t.vmid > vmid_lte:
            continue
        out.append(t)
    return out


def _smb_port_open(ipv6: str, timeout: float = 3.0) -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((ipv6, 445))
        return True
    except OSError:
        return False


def smb_path(target: VMTarget, c_drive_relative: str) -> str:
    """Build a UNC path for the target's c$ share. `c_drive_relative` is the
    path under C:\\ without leading slash (e.g. 'ProgramData\\NeuraVPS\\x.vbs').
    """
    rel = c_drive_relative.lstrip("\\/")
    return rf"\\{target.ipv6}\c$\{rel}"


def run_on_fleet(
    task_fn: TaskFn,
    *,
    only_vmids: Optional[List[int]] = None,
    skip_vmids: Optional[List[int]] = None,
    vmid_gte: Optional[int] = None,
    vmid_lte: Optional[int] = None,
    dry_run: bool = False,
    port_check_timeout: float = 3.0,
    smb_connect_timeout: int = 10,
    logger: Optional[logging.Logger] = None,
) -> List[TaskResult]:
    logger = logger or _default_logger()

    db = _init_firestore()
    all_targets = _list_running_targets(db, logger)
    targets = _apply_filters(
        all_targets, only_vmids, skip_vmids, vmid_gte, vmid_lte
    )
    logger.info(
        "running=%d  after-filter=%d  dry_run=%s",
        len(all_targets),
        len(targets),
        dry_run,
    )

    if dry_run:
        for t in targets:
            logger.info("[dry-run] vmid=%s ipv6=%s node=%s", t.vmid, t.ipv6, t.node_id)
        return []

    results: List[TaskResult] = []
    for t in targets:
        if not _smb_port_open(t.ipv6, timeout=port_check_timeout):
            logger.warning("vmid=%s: port 445 unreachable on %s", t.vmid, t.ipv6)
            results.append(TaskResult(t.vmid, "unreachable", "port 445 closed"))
            continue
        logger.info(
            "vmid=%s firestore_id=%s ipv6=%s username=%r share=\\\\%s\\c$",
            t.vmid,
            t.firestore_id,
            t.ipv6,
            t.username,
            t.ipv6,
        )
        try:
            smbclient.register_session(
                t.ipv6,
                username=t.username,
                password=t.password,
                connection_timeout=smb_connect_timeout,
            )
        except (SMBException, OSError, ValueError) as e:
            logger.warning("vmid=%s: SMB session failed: %s", t.vmid, e)
            results.append(TaskResult(t.vmid, "error", f"smb session: {e}"))
            continue

        try:
            result = task_fn(t)
        except Exception as e:  # task should normally return TaskResult itself
            logger.exception("vmid=%s: unhandled task exception", t.vmid)
            result = TaskResult(t.vmid, "error", f"task exception: {e}")
        finally:
            try:
                smbclient.delete_session(t.ipv6)
            except Exception:
                pass

        results.append(result)
        logger.info("vmid=%s: %s - %s", result.vmid, result.status, result.message)

    _print_summary(results, logger)
    return results


def _print_summary(results: List[TaskResult], logger: logging.Logger) -> None:
    by_status: dict[str, list[TaskResult]] = {}
    for r in results:
        by_status.setdefault(r.status, []).append(r)
    logger.info("=== Summary ===")
    for status in ("ok", "skipped", "unreachable", "error"):
        items = by_status.get(status, [])
        logger.info("  %-12s %d", status, len(items))
    failures = by_status.get("error", []) + by_status.get("unreachable", [])
    if failures:
        logger.info("Failures:")
        for r in failures:
            logger.info("  vmid=%s: %s - %s", r.vmid, r.status, r.message)


def _default_logger() -> logging.Logger:
    logger = logging.getLogger("smb_fleet")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def add_filter_args(parser: argparse.ArgumentParser) -> None:
    """Mount --only / --skip / --gte / --lte / --dry-run on `parser`."""
    parser.add_argument(
        "--only",
        help="comma-separated VMIDs to include (default: all running)",
    )
    parser.add_argument(
        "--skip",
        help="comma-separated VMIDs to exclude",
    )
    parser.add_argument("--gte", type=int, help="only VMIDs >= N")
    parser.add_argument("--lte", type=int, help="only VMIDs <= N")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list filtered targets and exit without connecting",
    )


def parsed_filters(args: argparse.Namespace) -> dict[str, Any]:
    """Translate parsed argparse filter args into kwargs for run_on_fleet."""
    return {
        "only_vmids": _split_int_csv(getattr(args, "only", None)),
        "skip_vmids": _split_int_csv(getattr(args, "skip", None)),
        "vmid_gte": getattr(args, "gte", None),
        "vmid_lte": getattr(args, "lte", None),
        "dry_run": bool(getattr(args, "dry_run", False)),
    }


def _split_int_csv(value: Optional[str]) -> Optional[List[int]]:
    if not value:
        return None
    return [int(x.strip()) for x in value.split(",") if x.strip()]
