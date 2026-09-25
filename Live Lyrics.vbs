' Live Lyrics starten – vollkommen ohne sichtbares Fenster (auch kein kurzes Aufblitzen).
' Einfach diese Datei doppelklicken. Für eine Desktop-Verknüpfung: Rechtsklick -> "Senden an" -> "Desktop (Verknüpfung erstellen)".

Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")
strFolder = objFSO.GetParentFolderName(WScript.ScriptFullName)

pythonw = """" & strFolder & "\venv\Scripts\pythonw.exe"""
script  = """" & strFolder & "\lyrics_gui.py"""

objShell.CurrentDirectory = strFolder
objShell.Run pythonw & " " & script, 0, False
