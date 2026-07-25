@echo off
REM Stop / turn off the KRUN RAG FastAPI server on 127.0.0.1:8765.
REM Delegates to stop_api.ps1 (graceful /shutdown, then force-kill listener
REM + sweep any leftover uvicorn process for this app). Safe if not running.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_api.ps1" -Port 8765
