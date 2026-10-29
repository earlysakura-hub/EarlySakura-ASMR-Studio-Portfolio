@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -B -X utf8 "build_exe.py"
if errorlevel 1 pause
