@echo off
cd /d "%~dp0"
echo Starting Abroad v3 on port 8080 ...
start "" http://127.0.0.1:8080
python -m uvicorn api:app --port 8080
pause
