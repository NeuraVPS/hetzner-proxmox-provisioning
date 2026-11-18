"""Static checks for the SQX versions the storage-box installer accepts."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "windows_vm/installers/install_sqx_from_storagebox.ps1").read_text()


def test_validate_set_lists_every_supported_version():
    m = re.search(r"\[ValidateSet\(([^)]*)\)\]\s*\[string\]\$Version = '(\d+)'", TEXT)
    assert m, "Version parameter not found"
    versions = re.findall(r"'(\d+)'", m.group(1))
    assert versions == ["142", "143", "144", "145"]
    # The default install is still 143; 144/145 are opt-in from the panel.
    assert m.group(2) == "143"


def test_145_reads_its_heap_from_strategyquantx_config_like_144():
    # v145 (build 3228) ships no StrategyQuantX_nocheck.* files, like v144.
    assert (
        "$xmxConfigName = if ($Version -in @('144', '145')) "
        "{ 'StrategyQuantX.config' } else { 'StrategyQuantX_nocheck.config' }"
    ) in TEXT


def test_145_gets_no_electron28_gpu_fix_and_no_debugger():
    # v145 ships Electron 44; the GPU-sandbox fix is only for Electron 28.
    assert "$SqxUiGpuSandboxVersions = @('142', '143')" in TEXT
    # The IFEO Debugger is only ever written for the v142/v143 _nocheck exe.
    assert "if ($exeName -eq 'StrategyQuantX_nocheck.exe') {" in TEXT
