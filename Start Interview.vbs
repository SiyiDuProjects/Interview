Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
command = "cmd.exe /c cd /d """ & root & "\apps\desktop"" && npm.cmd run start:desktop"
shell.Run command, 0, False
