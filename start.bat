@echo off
rem Starts the API Cost Monitor: backend (8100) and frontend (5173), each in its own window.
rem Stop: close both windows (or press Ctrl+C in each).
set "ROOT=%~dp0"

start "Backend 8100" cmd /k "cd /d "%ROOT%backend" && venv\Scripts\python -m alembic upgrade head && venv\Scripts\python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8100 --workers 1"

start "Frontend 5173" cmd /k "cd /d "%ROOT%frontend" && npm run dev"

rem Give both servers a few seconds to start, then open the dashboard.
timeout /t 8 /nobreak >nul
start "" "http://localhost:5173/vouchers"
