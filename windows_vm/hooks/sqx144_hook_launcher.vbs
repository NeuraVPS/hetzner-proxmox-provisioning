Option Explicit

Dim shell, fso, args
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set args = WScript.Arguments

If args.Count < 1 Then
  WScript.Quit 2
End If

Dim target, forwardArgs
target = Replace(CStr(args(0)), """", "")
If Not fso.FileExists(target) Then
  WScript.Quit 3
End If

forwardArgs = BuildForwardArgs(args)

Dim env, previousJava, hadJava
Set env = shell.Environment("PROCESS")
hadJava = False

Dim ifeoKey, debuggerPath, debuggerValue, hasDebugger
ifeoKey = "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX.exe"
debuggerPath = ifeoKey & "\Debugger"
hasDebugger = False

On Error Resume Next
debuggerValue = shell.RegRead(debuggerPath)
If Err.Number = 0 And Len(debuggerValue) > 0 Then hasDebugger = True
Err.Clear

previousJava = env("JAVA_TOOL_OPTIONS")
If Err.Number = 0 And Len(previousJava) > 0 Then hadJava = True
Err.Clear
On Error GoTo 0

env("JAVA_TOOL_OPTIONS") = "-Djava.awt.headless=true"

If hasDebugger Then
  shell.Run "reg delete " & QuoteArg(ifeoKey) & " /v Debugger /f", 0, True
End If

LaunchTarget shell, fso, target, forwardArgs

On Error Resume Next
If hadJava Then
  env("JAVA_TOOL_OPTIONS") = previousJava
Else
  env("JAVA_TOOL_OPTIONS") = ""
End If
Err.Clear
On Error GoTo 0

If hasDebugger Then
  shell.Run "reg add " & QuoteArg(ifeoKey) & " /v Debugger /t REG_SZ /d " & QuoteArg(debuggerValue) & " /f", 0, True
End If

Function QuoteArg(value)
  QuoteArg = """" & Replace(value, """", """""") & """"
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
  ' caller puts the Debugger value back. shellObj.Run is asynchronous, so
  ' without this the re-add can land BEFORE the child's CreateProcess reads
  ' IFEO and the child gets re-intercepted by our own hook — a launch that
  ' spins forever with no window (MetaTrader case 2026-07-22). The
  ' cross-process lock does not cover it: the race is inside one invocation.
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
