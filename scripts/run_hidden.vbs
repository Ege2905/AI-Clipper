' Starts Clip Studio in the background without any window (used by autostart).
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = base
sh.Run """" & base & "\venv\Scripts\pythonw.exe"" """ & base & "\launcher.py"" --background", 0, False
