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
ifeoKey = "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\StrategyQuantX_nocheck.exe"
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
  shellObj.Run launchCmd, 1, False

  On Error Resume Next
  shellObj.CurrentDirectory = previousCwd
  Err.Clear
  On Error GoTo 0
End Sub
