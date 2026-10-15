#!/usr/bin/env python3
"""Static checks for the SQX 142/143 UI GPU-sandbox fix in the SQX installer
(Windows 26100.33650 / KB5126645, NeuraVPS VM718 2026-10-15)."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "windows_vm/installers/install_sqx_from_storagebox.ps1").read_text()
FN = TEXT[TEXT.index("function Install-SqxUiGpuSandboxFix"):TEXT.index("$SqxUiGpuSandboxVersions = ")]
MAIN = TEXT[TEXT.index("try {\n    Connect-StorageBoxUnc"):]


def test_only_142_and_143_after_the_launch_configuration():
    assert "$SqxUiGpuSandboxVersions = @('142', '143')" in TEXT
    i = MAIN.index("Install-SqxLaunchConfiguration -InstallRoot $ExtractRoot")
    j = MAIN.index("if ($Version -in $SqxUiGpuSandboxVersions) {\n        Install-SqxUiGpuSandboxFix -InstallRoot $ExtractRoot")
    assert i < j < MAIN.index("New-ShellShortcutLnk")


def test_exactly_one_switch_and_nothing_wider():
    assert FN.count("appendSwitch(") == 1
    assert "$line = \"app.commandLine.appendSwitch('disable-gpu-sandbox');\"" in FN
    assert not re.search(r"no-sandbox'|in-process-gpu|disable-web-security", FN)


def test_never_deletes_and_never_fails_the_install():
    assert not re.search(r"(?i)Remove-Item|-Recurse|\.Delete\(|\brd\b|\bdel\b", FN)
    assert "Rename-Item -LiteralPath $asar -NewName $bakName" in FN
    # every failure lands in the catch, which only warns and renames back
    catch = FN[FN.index("} catch {"):]
    assert "Write-Warning" in catch and "throw" not in catch
    assert "Rename-Item -LiteralPath $bak -NewName 'app.asar'" in catch


def test_asar_entries_are_checked_and_user_is_not_touched():
    for check in ("unsafe asar entry name", "$name -eq '..'", "unsupported asar entry",
                  "asar entry out of range", "integrity mismatch", "backup hash mismatch"):
        assert check in FN, check
    assert not re.search(r"(?i)\buser\\", FN)
    assert "$rs = Join-Path $InstallRoot 'internal\\electron\\resources'" in FN


def test_rerun_over_a_fixed_install_sets_the_old_folder_aside():
    assert "Rename-Item -LiteralPath $appDir -NewName ('app.prev-nv' + $tag)" in FN
    assert "exists and is not ours" in FN
