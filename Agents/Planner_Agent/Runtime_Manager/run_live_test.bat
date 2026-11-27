@echo off
REM Double-click: starts GoModel from C:\Projects\NTG and runs the BMAD adapter live test.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_live_test.ps1"
