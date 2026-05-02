@echo off
REM Launch the KRUN RAG FastAPI server for the Obsidian plugin.
REM Default: 127.0.0.1:8765 (localhost only).
cd /d "%~dp0\.."
uv run uvicorn apps.fastapi_server:app --host 127.0.0.1 --port 8765 %*
