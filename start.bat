@echo off
rem Starts the API Cost Monitor: backend (8100) and frontend (5173), each in its own window,
rem plus the local AdhyaapakAI backend (8001) that Account Deletions talks to
rem (ADHYAAPAK_API_BASE_URL in backend\.env) when it isn't already running.
rem Stop: close the windows (or press Ctrl+C in each).
set "ROOT=%~dp0"
set "ADHYAAPAK=%ROOT%..\adhyaapak-ai\backend"

rem Any HTTP answer on 8001 means it is already up (curl.exe ships with Windows 10/11).
curl.exe -s -o nul --max-time 3 http://127.0.0.1:8001/
if errorlevel 1 (
  if exist "%ADHYAAPAK%\.venv\Scripts\python.exe" (
    start "AdhyaapakAI 8001" cmd /k "cd /d "%ADHYAAPAK%" && .venv\Scripts\python -m uvicorn server:app --host 127.0.0.1 --port 8001"
  ) else (
    echo AdhyaapakAI backend not found at %ADHYAAPAK%; Account Deletions will show "not reachable".
  )
) else (
  echo AdhyaapakAI backend already running on port 8001.
)

start "Backend 8100" cmd /k "cd /d "%ROOT%backend" && venv\Scripts\python -m alembic upgrade head && venv\Scripts\python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8100 --workers 1"

start "Frontend 5173" cmd /k "cd /d "%ROOT%frontend" && npm run dev"

rem Give the servers a few seconds to start, then open the dashboard.
timeout /t 8 /nobreak >nul
start "" "http://localhost:5173/"
