@echo off
cd /d "%~dp0"
REM pythonw.exe startet ohne Konsolenfenster (im Gegensatz zu python.exe)
start "" "venv\Scripts\pythonw.exe" "lyrics_gui.py"
