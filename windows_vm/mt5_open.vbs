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
