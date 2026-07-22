@echo off
title LiveCaptions Server
cd /d "%~dp0"
echo Starting LiveCaptions Server...
echo Open http://127.0.0.1:8000 in your browser once started.
echo.
.\venv\Scripts\python.exe server.py
pause
