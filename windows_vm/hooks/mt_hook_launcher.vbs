Option Explicit

Dim shell, fso, args
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set args = WScript.Arguments

Dim lockDir
lockDir = "C:\ProgramData\NeuraVPS\mt_hook.lock.d"

' Installations that must NOT be forced into portable mode: one directory per
' line, '#' comments and blanks ignored. Written once per VM by
' base/mt_portable_optout_sweep.py, which decides from the AppData side
' (origin.txt + EA counts + terminal logs) rather than guessing at launch.
'
' Why an external list instead of a check here: a legacy terminal opened even
' once under the forced /portable writes a stub config\accounts.ini into its
' install directory, so from that moment the install dir *looks* populated and
' any launch-time heuristic keeps hiding the customer's real data. The decision
' has to be made with the AppData side in view, and only once.
'
' Missing file = no opt-outs = force /portable exactly as before, so a fresh
' VM keeps the invariant with no extra state.
Dim optOutFile
optOutFile = "C:\ProgramData\NeuraVPS\mt_portable_optout.txt"

If args.Count < 1 Then
  WScript.Quit 2
End If

Dim target, exeName, ifeoKey, forwardArgs, launchTarget, isUpdate
target = Replace(CStr(args(0)), """", "")
exeName = LCase(fso.GetFileName(target))
ifeoKey = ""
isUpdate = HasUpdateFlag(args)
forwardArgs = BuildForwardArgs(args)
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

' Decide /portable only now: it depends on the RESOLVED installation, which
' ResolveMT5Target may have redirected. /update* runs keep the original command
' line untouched, as before.
If Not isUpdate Then
  If Not ArgsHavePortable(args) Then
    If Not IsPortableOptedOut(launchTarget) Then
      If Len(forwardArgs) > 0 Then forwardArgs = forwardArgs & " "
      forwardArgs = forwardArgs & "/portable"
    End If
  End If
End If

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
shell.Run launchCmd, 1, False

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

Function BuildForwardArgs(arguments)
  Dim i, parts
  parts = ""

  For i = 1 To arguments.Count - 1
    If Len(parts) > 0 Then parts = parts & " "
    parts = parts & FormatForwardArg(CStr(arguments(i)))
  Next

  BuildForwardArgs = parts
End Function

Function ArgsHavePortable(arguments)
  Dim i
  ArgsHavePortable = False
  For i = 1 To arguments.Count - 1
    If LCase(Trim(CStr(arguments(i)))) = "/portable" Then
      ArgsHavePortable = True
      Exit Function
    End If
  Next
End Function

' True when this installation's real data lives in %APPDATA% and forcing
' /portable would hide it. See the note on optOutFile above.
Function IsPortableOptedOut(exePath)
  Dim installDir, stream, line
  IsPortableOptedOut = False

  On Error Resume Next
  installDir = NormalizeDir(fso.GetParentFolderName(exePath))
  If Err.Number <> 0 Then Err.Clear : Exit Function
  If Len(installDir) = 0 Then Exit Function
  If Not fso.FileExists(optOutFile) Then Exit Function

  Set stream = fso.OpenTextFile(optOutFile, 1, False)
  If Err.Number <> 0 Then Err.Clear : Exit Function
  On Error GoTo 0

  Do Until stream.AtEndOfStream
    line = Trim(stream.ReadLine)
    If Len(line) > 0 And Left(line, 1) <> "#" Then
      If NormalizeDir(line) = installDir Then
        IsPortableOptedOut = True
        Exit Do
      End If
    End If
  Loop
  stream.Close
End Function

Function NormalizeDir(value)
  Dim v
  v = LCase(Trim(Replace(CStr(value), """", "")))
  Do While Len(v) > 0 And Right(v, 1) = "\"
    v = Left(v, Len(v) - 1)
  Loop
  NormalizeDir = v
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
