#!/usr/bin/env bash
# Start Thai PDF Studio at http://localhost:8000
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
echo "เปิดเบราว์เซอร์ไปที่ http://localhost:8000"
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
