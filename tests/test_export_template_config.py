#!/usr/bin/env python3
"""Static/fixture checks for the destructive boundaries of template export."""
from pathlib import Path
import subprocess
import tempfile
import os


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


def run_normalizer(text: str) -> str:
    lines = SOURCE.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("normalize_current_config()"))
    body = "\n".join(lines[start : start + 7])
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(text)
        name = f.name
    try:
        return subprocess.check_output(
            ["bash", "-c", f"{body}\nnormalize_current_config '{name}'"], text=True
        )
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
    normalized = run_normalizer(
        "cpu: x86-64-v4\nparent: 100\nlock: snapshot\nvmstate: foo\nsnaptime: 1\nscsi0: vm-100-disk-0\n"
    )
    assert normalized == "cpu: x86-64-v4\nscsi0: vm-100-disk-0\n"
    for name in ("install_sqx_from_storagebox.ps1", "install_mt_from_storagebox.ps1"):
        text = (ROOT / "windows_vm/installers" / name).read_text()
        assert "files-hel.neuravps.com/pkg" in text
        assert "files-fsn.neuravps.com/pkg" in text
        assert "raw.githubusercontent.com" not in text[text.find("function Install-"):]
        assert "Get-FileHash" in text
    cache = (ROOT / "base/snippets/nvx-installers.sh").read_text()
    assert "HOOK_DIR=\"$DESTINO/hooks/$HOOK_REVISION\"" in cache
    assert "sha256sum" in cache
    restore = (ROOT / "scripts/restore_template_vm_from_shared_storage.sh").read_text()
    assert "neuravps-stream-template-key" in restore
    assert "^[A-Za-z0-9_-]+$" in restore
    # Exercise nvx-installers' all-or-nothing installer staging with a fake
    # downloader: a failed third installer must leave existing PS1 files byte
    # for byte unchanged.
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "pkg"
        dest.mkdir()
        old = {name: (dest / name) for name in (
            "install_mt_from_storagebox.ps1",
            "install_sqx_from_storagebox.ps1",
            "install_qa_from_storagebox.ps1",
        )}
        for path in old.values():
            path.write_text("old-" + path.name)
        hooks = dest / "hooks" / "1ecfca1bdb5b4e981f9ed9c7f66f471a911611d"
        hooks.mkdir(parents=True)
        for name in ("sqx_hook_launcher.vbs", "mt_hook_launcher.vbs"):
            (hooks / name).write_bytes((ROOT / "windows_vm/hooks" / name).read_bytes())
        fake = Path(td) / "curl"
        fake.write_text(
            "#!/bin/sh\n"
            "out=\"\"; for a in \"$@\"; do [ \"$prev\" = -o ] && out=\"$a\"; prev=\"$a\"; done\n"
            "for a in \"$@\"; do case \"$a\" in *install_qa*) exit 1;; *sqx_hook_launcher.vbs*) cp \"$TEST_ROOT/windows_vm/hooks/sqx_hook_launcher.vbs\" \"$out\";; *mt_hook_launcher.vbs*) cp \"$TEST_ROOT/windows_vm/hooks/mt_hook_launcher.vbs\" \"$out\";; esac; done\n"
            "[ -s \"$out\" ] || head -c 6000 /dev/zero >\"$out\"\n"
        )
        fake.chmod(0o755)
        env = os.environ.copy(); env.update({"PATH": f"{td}:{env['PATH']}", "DESTINO": str(dest), "TEST_ROOT": str(ROOT)})
        subprocess.run(["bash", str(ROOT / "base/snippets/nvx-installers.sh")], env=env, check=False)
        assert all(path.read_text() == "old-" + path.name for path in old.values())
    print("export template fixture checks passed")


if __name__ == "__main__":
    main()
