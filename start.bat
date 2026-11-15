@echo off
rem Start ClipEditor with a console window (useful for troubleshooting).
rem The browser opens automatically; keep this window open – closing it quits ClipEditor.
rem For the app experience without a console, use ClipEditor.pyw or the shortcut instead.
cd /d "%~dp0"
title ClipEditor
set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% app.py
if errorlevel 1 pause
