#!/usr/bin/env bash
# Deploy mt5_open.vbs to running Windows VMs on all nodes via SSH + qemu-guest-agent.
# Self-contained: VBS content is embedded below. For each node, SSHs in and for each
# running VM replaces C:\MetaTrader\mt5_open.vbs if it exists.

set -euo pipefail

############################################################
# EMBEDDED mt5_open.vbs (same as scripts/mt5_open.vbs)
############################################################
EMBEDDED_VBS=$(cat <<'VBSEOF'
' MT5 launcher: open MQL5/EX5 files and URLs with the correct MetaTrader 001-010 instance.
' Usage: wscript.exe mt5_open.vbs "MODE" "%1"
' Modes: ex5 | editor | import | terminal

Option Explicit

Const MT_BASE = "C:\MetaTrader\MetaTrader 5 - "

Dim fso, shell, mode, pathArg, resolvedPath, folderNum, basePath, exePath, args, fullCmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

If WScript.Arguments.Count < 2 Then
  WScript.Quit 1
End If

mode = WScript.Arguments(0)
pathArg = WScript.Arguments(1)

' Resolve path: if it looks like a file path, get canonical path and detect which MT folder it belongs to
resolvedPath = ResolvePath(fso, pathArg)
folderNum = GetFolderNum(resolvedPath)

basePath = MT_BASE & folderNum & "\"

Select Case LCase(mode)
  Case "ex5"
    exePath = basePath & "terminal64.exe"
    args = "/portable /ex5:""" & pathArg & """"
  Case "editor"
    exePath = basePath & "metaeditor64.exe"
    args = "/portable """ & pathArg & """"
  Case "import"
    exePath = basePath & "terminal64.exe"
    args = "/portable /import:""" & pathArg & """"
  Case "terminal"
    exePath = basePath & "terminal64.exe"
    args = "/portable """ & pathArg & """"
  Case Else
    WScript.Quit 2
End Select

' Run: exe path in quotes (for spaces), then args. Shell.Run expects one string.
fullCmd = """" & exePath & """ " & args
shell.Run fullCmd, 1, False

' After launch, wait 5s then re-apply our associations so MetaTrader cannot overwrite them.
WScript.Sleep 5000
RestoreRegistry shell, WScript.ScriptFullName

' ---
Sub RestoreRegistry(shell, scriptPath)
  Dim cmd, iconBase
  iconBase = "C:\MetaTrader\MetaTrader 5 - 001"
  cmd = "wscript.exe """ & scriptPath & """ "
  ' HKCU\...\shell\open\command = (Default) value; trailing \ means default in RegWrite
  shell.RegWrite "HKCU\Software\Classes\EX5.File\shell\open\command\", cmd & """ex5"" ""%1""", "REG_SZ"
  shell.RegWrite "HKCU\Software\Classes\MQL5.File\shell\open\command\", cmd & """editor"" ""%1""", "REG_SZ"
  shell.RegWrite "HKCU\Software\Classes\MQL5.Header\shell\open\command\", cmd & """editor"" ""%1""", "REG_SZ"
  shell.RegWrite "HKCU\Software\Classes\mql5buy\shell\open\command\", cmd & """terminal"" ""%1""", "REG_SZ"
  shell.RegWrite "HKCU\Software\Classes\metaeditor5\shell\open\command\", cmd & """editor"" ""%1""", "REG_SZ"
  shell.RegWrite "HKCU\Software\Classes\MetaTrader 5 Export File\shell\open\command\", cmd & """import"" ""%1""", "REG_SZ"
End Sub

' ---
Function ResolvePath(fso, pathArg)
  Dim p
  ResolvePath = pathArg
  If Len(pathArg) < 3 Then Exit Function
  ' Look like a path? (drive letter or UNC)
  If (Asc(pathArg) >= 65 And Asc(pathArg) <= 90) Or (Asc(pathArg) >= 97 And Asc(pathArg) <= 122) Then
    If Mid(pathArg, 2, 1) = ":" And Mid(pathArg, 3, 1) = "\" Then
      On Error Resume Next
      If fso.FileExists(pathArg) Then
        Set p = fso.GetFile(pathArg)
        ResolvePath = p.Path
      ElseIf fso.FolderExists(pathArg) Then
        Set p = fso.GetFolder(pathArg)
        ResolvePath = p.Path
      Else
        ResolvePath = fso.GetAbsolutePathName(pathArg)
      End If
      On Error GoTo 0
    End If
  End If
End Function

Function GetFolderNum(resolvedPath)
  Dim i, n, baseFolder, normalized
  GetFolderNum = "001"
  normalized = Replace(resolvedPath, "/", "\")
  ' Ensure trailing \ so folder path "C:\MetaTrader\MetaTrader 5 - 003" matches baseFolder "C:\MetaTrader\MetaTrader 5 - 003\"
  If Len(normalized) > 0 And Right(normalized, 1) <> "\" Then normalized = normalized & "\"
  For i = 1 To 10
    n = Right("00" & i, 3)
    baseFolder = MT_BASE & n & "\"
    If Len(normalized) >= Len(baseFolder) Then
      If StrComp(Left(normalized, Len(baseFolder)), baseFolder, 1) = 0 Then
        GetFolderNum = n
        Exit Function
      End If
    End If
  Next
End Function
VBSEOF
)

############################################################
# DEFINE REMOTE FUNCTION (runs on each node via SSH)
############################################################
remote_task() {
  echo "== Running remote task =="
  if [[ -z "${VBS_B64:-}" ]]; then
    echo "No VBS_B64 on remote." >&2
    return 1
  fi
  PS_SCRIPT="\$b64 = '$VBS_B64'; \$content = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String(\$b64)); if (Test-Path 'C:\MetaTrader\mt5_open.vbs') { \$enc = [System.Text.UTF8Encoding]::new(\$false); [System.IO.File]::WriteAllText('C:\MetaTrader\mt5_open.vbs', \$content, \$enc); Write-Output 'Updated' } else { Write-Output 'Skipped' }; exit 0"
  if command -v python3 &>/dev/null; then
    PS_B64=$(printf '%s' "$PS_SCRIPT" | python3 -c "import sys, base64; print(base64.b64encode(sys.stdin.read().encode('utf-16-le')).decode('ascii'))")
  elif command -v iconv &>/dev/null; then
    PS_B64=$(printf '%s' "$PS_SCRIPT" | iconv -f UTF-8 -t UTF-16LE | base64 -w0 2>/dev/null || { printf '%s' "$PS_SCRIPT" | iconv -f UTF-8 -t UTF-16LE | base64 | tr -d '\n'; })
  else
    echo "Need python3 or iconv on node." >&2
    return 1
  fi
  VMIDS=$(qm list 2>/dev/null | awk 'NR>1 && $3=="running" {print $1}' || true)
  if [[ -z "${VMIDS:-}" ]]; then
    echo "No running VMs on this node."
    echo "== Finished =="
    return 0
  fi
  for VMID in $VMIDS; do
    echo "------------------------------------------------"
    echo "VMID $VMID"
    if ! qm agent "$VMID" ping 2>/dev/null; then
      echo "  Skip (no agent or unreachable)"
      continue
    fi
    RAW=$(qm guest exec "$VMID" -- powershell.exe -NoProfile -EncodedCommand "$PS_B64" 2>&1)
    EXIT=$?
    if [[ $EXIT -eq 0 ]]; then
      if command -v jq &>/dev/null; then
        OUT=$(printf '%s' "$RAW" | jq -r '.["out-data"] // empty' 2>/dev/null | tr -d '\r' | sed '/^$/d')
        echo "  ${OUT:-$RAW}"
      elif command -v python3 &>/dev/null; then
        OUT=$(printf '%s' "$RAW" | python3 -c "import sys,json; d=json.load(sys.stdin); print((d.get('out-data') or '').replace(chr(13),'').strip())" 2>/dev/null)
        echo "  ${OUT:-$RAW}"
      else
        echo "  $RAW"
      fi
    else
      echo "  Failed: $RAW"
    fi
  done
  echo "== Finished =="
}

############################################################
# Encode embedded VBS once, then loop over nodes
############################################################
VBS_B64=$(printf '%s' "$EMBEDDED_VBS" | base64 -w0 2>/dev/null || printf '%s' "$EMBEDDED_VBS" | base64 | tr -d '\n')
FUNC_CONTENT=$(declare -f remote_task)

for ((i=1; i<=71; i++)); do
  HEX=$(printf "%x" "$i")
  IP="fd00:4000::${HEX}"

  echo "================================================"
  echo "Connecting to $IP ($i)"

  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ForwardAgent=yes -o ConnectTimeout=10 \
    "root@$IP" "VBS_B64='$VBS_B64'; $FUNC_CONTENT; remote_task" \
    || echo "Failed to connect to $IP"
done
