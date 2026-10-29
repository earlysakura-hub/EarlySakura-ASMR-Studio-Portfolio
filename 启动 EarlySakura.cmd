@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Missing .venv\Scripts\pythonw.exe
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -B -X utf8 "studio.py"
