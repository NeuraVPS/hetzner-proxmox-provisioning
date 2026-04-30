Option Explicit

Dim shell, fso, args
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set args = WScript.Arguments

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
shell.Run launchCmd, 1, False

On Error Resume Next
shell.CurrentDirectory = previousCwd
Err.Clear
On Error GoTo 0

If hasDebugger Then
  shell.Run "reg add " & QuoteArg(ifeoKey) & " /v Debugger /t REG_SZ /d " & QuoteArg(debuggerValue) & " /f", 0, True
End If

Function QuoteArg(value)
  QuoteArg = """" & Replace(value, """", """""") & """"
End Function

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
