Option Explicit

Dim shell, fso, args
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set args = WScript.Arguments

' Serializes delete/launch/re-add of whichever IFEO Debugger key this
' invocation is responsible for. Shared across both SQX exe names on purpose
' (mirrors mt_hook_launcher.vbs, which shares one lock across four exe names):
' the critical section is short, and a single lock is one less thing to get
' wrong than per-key locks would be.
Dim lockDir
lockDir = "C:\ProgramData\NeuraVPS\sqx_hook.lock.d"

If args.Count < 1 Then
  WScript.Quit 2
End If

Dim target
target = Replace(CStr(args(0)), """", "")
If Not fso.FileExists(target) Then
  WScript.Quit 3
End If

' Derive the recursion-guard IFEO key from the executable Windows is actually
' intercepting, never hardcode it.
'
' History: this script used to hardcode
' "...\Image File Execution Options\StrategyQuantX_nocheck.exe" as the guard
' key, because that was the only exe it was ever wired to. A second copy
' (sqx144_hook_launcher.vbs) hardcoded StrategyQuantX.exe instead — same
' script, different literal. That is fragile by construction: wiring
' StrategyQuantX.exe to a copy still carrying the _nocheck.exe literal clears
' the WRONG key, leaves its OWN key armed, and the relaunch re-enters through
' IFEO on itself — a fork bomb, not a hang. Deriving the key from
' fso.GetFileName(target) makes that class of mistake impossible: whichever
' exe Windows handed us is the exe whose key gets cleared and restored, in
' any install location, so one script now covers the v143 engine
' (StrategyQuantX_nocheck.exe) and the v144+ engine (StrategyQuantX.exe) and
' sqx144_hook_launcher.vbs can be retired.
'
' This does NOT by itself make it safe to wire StrategyQuantX.exe fleet-wide.
' See the withdrawal banner in README.md: on a box that also carries a v143
' install, StrategyQuantX.exe is ALSO v143's interactive launcher/checker,
' which re-execs itself under that same image name — and IFEO keys by name,
' not path, so a key meant for the v144 engine catches that self-relaunch too.
' That is an IFEO/name-collision problem this script cannot see or fix; only
' the caller (the install/sweep gate) can, by refusing to wire
' StrategyQuantX.exe on any box where a v143 StrategyQuantX.exe is also
' present.
Dim exeName, ifeoKey
exeName = LCase(fso.GetFileName(target))
ifeoKey = ""

Select Case exeName
  Case "strategyquantx_nocheck.exe"
    ifeoKey = "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe"
  Case "strategyquantx.exe"
    ifeoKey = "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX.exe"
  Case Else
    WScript.Quit 4
End Select

Dim forwardArgs
forwardArgs = BuildForwardArgs(args)

' SQX is Java/AWT; without headless it crashes on RDP reconnect/displayChanged
' (agent doc Section 9.9.15b "Mode A"). Set on THIS process only — shell.Run
' inherits it into the child, and it is restored (or cleared) below so this
' short-lived wscript host never leaves a stray machine/user-level value.
Dim env, previousJava, hadJava
Set env = shell.Environment("PROCESS")
hadJava = False

On Error Resume Next
previousJava = env("JAVA_TOOL_OPTIONS")
If Err.Number = 0 And Len(previousJava) > 0 Then hadJava = True
Err.Clear
On Error GoTo 0

env("JAVA_TOOL_OPTIONS") = "-Djava.awt.headless=true"

Dim gotLock
gotLock = AcquireLock()

' Read the value we are about to clear ONLY ONCE THE LOCK IS OURS, and restore
' it UNCONDITIONALLY afterwards (ported from mt_hook_launcher.vbs, PR#146,
' 2026-08): reading before the lock let a concurrent launcher's delete race
' ours and see an empty value; skipping the re-add on that empty read then
' left the Debugger key gone for good, which is a silent, permanent loss of
' the crash-immunity fix, not a crash. This script only runs because Windows
' found OUR launcher in that Debugger value, so the correct string is known
' from how we were started even when the read comes back empty.
Dim debuggerPath, debuggerValue
debuggerPath = ifeoKey & "\Debugger"
debuggerValue = ""

On Error Resume Next
debuggerValue = shell.RegRead(debuggerPath)
Err.Clear
On Error GoTo 0

If Len(debuggerValue) = 0 Then debuggerValue = CanonicalDebugger()

shell.Run "reg delete " & QuoteArg(ifeoKey) & " /v Debugger /f", 0, True

LaunchTarget shell, fso, target, forwardArgs

On Error Resume Next
If hadJava Then
  env("JAVA_TOOL_OPTIONS") = previousJava
Else
  env("JAVA_TOOL_OPTIONS") = ""
End If
Err.Clear
On Error GoTo 0

' Unconditional re-add: see the note above the read. Two launchers both
' restoring the same canonical string is harmless; one of them skipping it is
' not.
shell.Run "reg add " & QuoteArg(ifeoKey) & " /v Debugger /t REG_SZ /d " & QuoteArg(debuggerValue) & " /f", 0, True

If gotLock Then ReleaseLock()

Function AcquireLock()
  Dim iters
  iters = 0
  Do
    On Error Resume Next
    Err.Clear
    fso.CreateFolder(lockDir)
    If Err.Number = 0 Then
      On Error GoTo 0
      AcquireLock = True
      Exit Function
    End If
    Err.Clear
    If fso.FolderExists(lockDir) Then
      If DateDiff("s", fso.GetFolder(lockDir).DateLastModified, Now) > 12 Then
        fso.DeleteFolder lockDir, True
      End If
    End If
    On Error GoTo 0
    WScript.Sleep 35
    iters = iters + 1
  Loop While iters < 400
  AcquireLock = False
End Function

Sub ReleaseLock()
  On Error Resume Next
  If fso.FolderExists(lockDir) Then fso.DeleteFolder lockDir, True
  On Error GoTo 0
End Sub

Function QuoteArg(value)
  QuoteArg = """" & Replace(value, """", """""") & """"
End Function

' The Debugger string that starts THIS launcher for the given key, rebuilt
' from how we were actually started rather than hardcoded. Used only when the
' registry read above came back empty (a concurrent launcher had already
' cleared it), so the key is never left deleted. The host filename is forced
' to wscript.exe: under cscript we would otherwise write a Debugger that opens
' a console window on every SQX launch.
Function CanonicalDebugger()
  Dim hostDir
  hostDir = ""
  On Error Resume Next
  hostDir = fso.GetParentFolderName(WScript.FullName)
  Err.Clear
  On Error GoTo 0
  If Len(hostDir) = 0 Then hostDir = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32"
  CanonicalDebugger = """" & hostDir & "\wscript.exe"" """ & WScript.ScriptFullName & """"
End Function

Function BuildForwardArgs(arguments)
  Dim i, raw, parts
  parts = ""

  For i = 1 To arguments.Count - 1
    raw = CStr(arguments(i))
    If Len(parts) > 0 Then parts = parts & " "
    parts = parts & QuoteArg(raw)
  Next

  BuildForwardArgs = parts
End Function

Sub LaunchTarget(shellObj, fsoObj, exePath, argString)
  Dim previousCwd, launchDir, launchCmd
  previousCwd = shellObj.CurrentDirectory
  launchDir = fsoObj.GetParentFolderName(exePath)
  If Len(launchDir) > 0 Then
    On Error Resume Next
    shellObj.CurrentDirectory = launchDir
    Err.Clear
    On Error GoTo 0
  End If

  launchCmd = QuoteArg(exePath)
  If Len(argString) > 0 Then launchCmd = launchCmd & " " & argString

  ' Count first, launch, then wait for the child to actually exist before the
  ' Debugger value goes back. shellObj.Run is asynchronous, so without this
  ' the re-add above could land BEFORE the child's CreateProcess reads IFEO —
  ' and the child would be re-intercepted by our own hook, spawning another
  ' wscript instead of SQX (same self-race as MetaTrader's 2026-07-22 case).
  ' The cross-process lock does not cover this: the race is inside one
  ' invocation, not between two.
  Dim childName, beforeCount
  childName = fsoObj.GetFileName(exePath)
  beforeCount = CountProcesses(childName)

  shellObj.Run launchCmd, 1, False

  WaitForNewProcess childName, beforeCount, 15000

  On Error Resume Next
  shellObj.CurrentDirectory = previousCwd
  Err.Clear
  On Error GoTo 0
End Sub

' How many processes with this image name are running right now. Returns -1 if
' WMI is unavailable, which the caller treats as "can't tell" and falls back to
' a fixed pause rather than re-arming the hook immediately.
Function CountProcesses(exeName)
  Dim wmi, col
  CountProcesses = -1
  On Error Resume Next
  Set wmi = GetObject("winmgmts:\\.\root\cimv2")
  If Err.Number <> 0 Then Err.Clear : Exit Function
  Set col = wmi.ExecQuery("SELECT ProcessId FROM Win32_Process WHERE Name = '" & _
                          Replace(exeName, "'", "''") & "'")
  If Err.Number <> 0 Then Err.Clear : Exit Function
  CountProcesses = col.Count
  If Err.Number <> 0 Then Err.Clear : CountProcesses = -1
  On Error GoTo 0
End Function

' Block until one more `exeName` exists than there was before the launch, or
' the timeout expires.
Sub WaitForNewProcess(exeName, beforeCount, timeoutMs)
  Dim waited, now_
  If beforeCount < 0 Then
    ' No WMI to observe with: a fixed pause is still far better than re-adding
    ' the Debugger value the instant after an async launch.
    WScript.Sleep 3000
    Exit Sub
  End If
  waited = 0
  Do While waited < timeoutMs
    now_ = CountProcesses(exeName)
    If now_ > beforeCount Then Exit Sub
    If now_ < 0 Then WScript.Sleep 3000 : Exit Sub
    WScript.Sleep 150
    waited = waited + 150
  Loop
End Sub
