@echo off
REM Launch the KRUN RAG FastAPI server for the Obsidian plugin.
REM Default: 127.0.0.1:8765 (localhost only).
cd /d "%~dp0\.."
REM bge-m3 embedding model is already cached locally. Skip HuggingFace Hub
REM online revision checks (~9s per boot) and the unauthenticated-request
REM warning. If you ever need to re-download / update the model, comment
REM these two lines out for one run so the Hub is reachable again.
set HF_HUB_OFFLINE=1
set TRANSFORMERS_OFFLINE=1
uv run uvicorn apps.fastapi_server:app --host 127.0.0.1 --port 8765 %*
