@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
start "" http://localhost:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
