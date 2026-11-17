@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto :failed
)
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, yt_dlp, yt_dlp_ejs, imageio_ffmpeg" >nul 2>&1
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -r requirements-worker.txt
  if errorlevel 1 goto :failed
)
echo.
echo Buka http://127.0.0.1:8000 di browser.
".venv\Scripts\python.exe" run.py
pause
exit /b
:failed
echo Instalasi gagal. Pastikan Python 3.11+ tersedia dan internet tersambung.
pause
exit /b 1
