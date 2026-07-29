Option Explicit

Dim shell, fso, args
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set args = WScript.Arguments

Dim lockDir
lockDir = "C:\ProgramData\NeuraVPS\mt_hook.lock.d"

If args.Count < 1 Then
  WScript.Quit 2
End If

Dim target, exeName, ifeoKey, forwardArgs, launchTarget, isUpdate
target = Replace(CStr(args(0)), """", "")
exeName = LCase(fso.GetFileName(target))
ifeoKey = ""
isUpdate = HasUpdateFlag(args)
forwardArgs = BuildForwardArgs(args, isUpdate)
launchTarget = target

Select Case exeName
  Case "terminal64.exe"
    ifeoKey = "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal64.exe"
    If Not isUpdate Then launchTarget = ResolveMT5Target(forwardArgs, "terminal64.exe", target)
  Case "metaeditor64.exe"
    ifeoKey = "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor64.exe"
    If Not isUpdate Then launchTarget = ResolveMT5Target(forwardArgs, "metaeditor64.exe", target)
  Case "terminal.exe"
    ifeoKey = "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\terminal.exe"
    If Not isUpdate Then launchTarget = ResolveMT5Target(forwardArgs, "terminal.exe", target)
  Case "metaeditor.exe"
    ifeoKey = "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\metaeditor.exe"
    If Not isUpdate Then launchTarget = ResolveMT5Target(forwardArgs, "metaeditor.exe", target)
  Case Else
    WScript.Quit 4
End Select

Dim debuggerPath, debuggerValue, hasDebugger
debuggerPath = ifeoKey & "\Debugger"
hasDebugger = False

On Error Resume Next
debuggerValue = shell.RegRead(debuggerPath)
If Err.Number = 0 Then hasDebugger = True
Err.Clear
On Error GoTo 0

' Serialize delete/launch/re-add of the shared IFEO Debugger key so that many
' MetaTrader terminals auto-starting at logon cannot race it (a race lets the
' re-added key re-intercept a concurrent relaunch -> wscript/reg fork-bomb).
Dim gotLock
gotLock = AcquireLock()

shell.Run "reg delete " & QuoteArg(ifeoKey) & " /v Debugger /f", 0, True

Dim previousCwd, launchDir, launchCmd
previousCwd = shell.CurrentDirectory
launchDir = fso.GetParentFolderName(launchTarget)
If Len(launchDir) > 0 Then
  On Error Resume Next
  shell.CurrentDirectory = launchDir
  Err.Clear
  On Error GoTo 0
End If

launchCmd = QuoteArg(launchTarget)
If Len(forwardArgs) > 0 Then launchCmd = launchCmd & " " & forwardArgs

' Count the target's processes BEFORE launching, so the wait below can tell a
' newly-started child from one that was already running.
Dim childName, beforeCount
childName = fso.GetFileName(launchTarget)
beforeCount = CountProcesses(childName)

shell.Run launchCmd, 1, False

' Wait for the child to actually exist before the Debugger value goes back.
'
' This is the fix for the self-race that made MetaTrader hang on launch with a
' spinning cursor and no window (real case 2026-07-22): shell.Run is
' asynchronous, so without this the re-add below could land BEFORE the child's
' CreateProcess read IFEO — and the child was then re-intercepted by our own
' hook, spawning another wscript instead of a terminal. The cross-process lock
' does not help: that race is INSIDE a single invocation. Post-update CPU load
' widens the window, and every double-click re-arms it.
WaitForNewProcess childName, beforeCount, 15000

On Error Resume Next
shell.CurrentDirectory = previousCwd
Err.Clear
On Error GoTo 0

If hasDebugger Then
  shell.Run "reg add " & QuoteArg(ifeoKey) & " /v Debugger /t REG_SZ /d " & QuoteArg(debuggerValue) & " /f", 0, True
End If

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
' the timeout expires. Returns True if the child was seen.
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

Function BuildForwardArgs(arguments, preserveOriginal)
  Dim i, raw, item, parts, hasPortable
  parts = ""
  hasPortable = False

  For i = 1 To arguments.Count - 1
    raw = CStr(arguments(i))
    item = Trim(raw)
    If LCase(item) = "/portable" Then hasPortable = True
    If Len(parts) > 0 Then parts = parts & " "
    parts = parts & FormatForwardArg(raw)
  Next

  If Not preserveOriginal And Not hasPortable Then
    If Len(parts) > 0 Then parts = parts & " "
    parts = parts & "/portable"
  End If

  BuildForwardArgs = parts
End Function

' MT5's /updateadmin parser scans GetCommandLineW() looking for /flag:"value"
' (quotes around the value, not around the whole arg). Wrapping the entire
' "/path:VALUE" in quotes makes it grab past the closing quote into the next
' token and produces a corrupted path. Preserve the original shape instead.
Function FormatForwardArg(value)
  Dim colonPos, flagPart, valuePart
  If Len(value) > 0 And Left(value, 1) = "/" Then
    colonPos = InStr(value, ":")
    If colonPos > 1 Then
      flagPart = Left(value, colonPos)
      valuePart = Mid(value, colonPos + 1)
      If NeedsQuoting(valuePart) Then
        FormatForwardArg = flagPart & QuoteArg(valuePart)
      Else
        FormatForwardArg = value
      End If
      Exit Function
    End If
  End If
  If NeedsQuoting(value) Then
    FormatForwardArg = QuoteArg(value)
  Else
    FormatForwardArg = value
  End If
End Function

Function NeedsQuoting(value)
  NeedsQuoting = False
  If Len(value) = 0 Then NeedsQuoting = True : Exit Function
  If InStr(value, " ") > 0 Then NeedsQuoting = True : Exit Function
  If InStr(value, vbTab) > 0 Then NeedsQuoting = True : Exit Function
  If InStr(value, """") > 0 Then NeedsQuoting = True : Exit Function
End Function

Function HasUpdateFlag(arguments)
  Dim i, item
  HasUpdateFlag = False
  For i = 1 To arguments.Count - 1
    item = LCase(Trim(CStr(arguments(i))))
    If Len(item) >= 7 Then
      If Left(item, 7) = "/update" Then
        HasUpdateFlag = True
        Exit Function
      End If
    End If
  Next
End Function

Function ResolveMT5Target(forwardArgs, executableName, defaultTarget)
  Dim i, raw, candidate, normalizedCandidate, subFolder, basePath, exePath
  ResolveMT5Target = defaultTarget

  For i = 1 To args.Count - 1
    raw = CStr(args(i))
    candidate = Trim(raw)
    candidate = Replace(candidate, """", "")
    normalizedCandidate = NormalizePathArg(candidate)
    If Len(normalizedCandidate) > 0 Then
      If LCase(Left(normalizedCandidate, 14)) = "c:\metatrader\" Then
        subFolder = ExtractSubFolder(normalizedCandidate)
        If Len(subFolder) > 0 Then
          basePath = "C:\MetaTrader\" & subFolder
          exePath = basePath & "\" & executableName
          ResolveMT5Target = exePath
          Exit Function
        End If
      End If
    End If
  Next
End Function

Function NormalizePathArg(value)
  Dim p
  NormalizePathArg = value
  p = InStr(value, ":C:\")
  If p > 0 Then
    NormalizePathArg = Mid(value, p + 1)
  End If
End Function

Function ExtractSubFolder(pathValue)
  Dim remainder, pos
  ExtractSubFolder = ""
  If Len(pathValue) <= 13 Then Exit Function
  remainder = Mid(pathValue, 15)
  pos = InStr(remainder, "\")
  If pos > 1 Then
    ExtractSubFolder = Left(remainder, pos - 1)
  End If
End Function
