' Run scripts\service.py <command> with pythonw.exe and no console window.
' Usage: wscript.exe run_hidden.vbs [ensure|open|stop]
Option Explicit
Dim fso, sh, root, arg, py, env
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
arg = "ensure"
If WScript.Arguments.Count > 0 Then arg = WScript.Arguments(0)
py = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
  If arg = "open" Then MsgBox "Python environment not found. Please run start.bat once first.", 48, "AI Quant"
  WScript.Quit 1
End If
Set env = sh.Environment("PROCESS")
env("PYTHONUTF8") = "1"
env("PYTHONIOENCODING") = "utf-8"
sh.CurrentDirectory = root
sh.Run """" & py & """ """ & root & "\scripts\service.py"" " & arg, 0, False
