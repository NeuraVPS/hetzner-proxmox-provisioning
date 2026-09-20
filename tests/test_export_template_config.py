#!/usr/bin/env python3
"""Static/fixture checks for the destructive boundaries of template export."""
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/export_template_vm_to_shared_storage.sh"
SOURCE = SCRIPT.read_text()


def run_snapshot_guard(text: str) -> bool:
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(text)
        name = f.name
    try:
        program = r"/^\[[^]]+\]/{bad=1} /^[[:space:]]*(pending|snapshots):[[:space:]]/{bad=1} END{exit bad ? 0 : 1}"
        return subprocess.run(["awk", program, name]).returncode == 0
    finally:
        Path(name).unlink()


def main() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    assert "qm config \"$VMID\" --current" in SOURCE
    assert '"${SCP_BASE[@]}" "$EXPORT_CONF"' in SOURCE
    assert "not positively stopped" in SOURCE
    assert 'LEGACY_DIRECT_EXPORT="${LEGACY_DIRECT_EXPORT:-0}"' in SOURCE
    assert 'REMOTE_TEMPLATE_KEY="${TEMPLATE_KEY}-$(date -u +%Y%m%d)"' in SOURCE
    assert "neuravps-stream-template-key" in SOURCE
    assert "zfs destroy" not in SOURCE
    assert "SNAPSHOT_NAME=\"${SNAPSHOT_NAME:-export-" in SOURCE
    assert run_snapshot_guard("cpu: x86-64-v4\nscsi0: vm-100-disk-0\n") is False
    assert run_snapshot_guard(
        "cpu: x86-64-v4\nscsi0: vm-100-disk-0\n[snapshot-before]\nsnapname: before\n"
    ) is True
    assert run_snapshot_guard("cpu: x86-64-v4\npending: cpu: host\n") is True
    print("export template fixture checks passed")


if __name__ == "__main__":
    main()
