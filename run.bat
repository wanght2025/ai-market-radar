@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
setlocal
cd /d "%~dp0"
if not exist "config.json" copy /y "config.example.json" "config.json" >nul
if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3.12 -m venv .venv
  ) else (
    where python >nul 2>nul
    if errorlevel 1 (
      echo Please install Python 3.12 from python.org, then run this file again.
      pause
      exit /b 1
    )
    python -m venv .venv
  )
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -c "import importlib.metadata as m; from pathlib import Path; assert all(m.version(n)==v for n,v in (line.split()[0].split('==') for line in Path('requirements.lock.txt').read_text().splitlines() if line.strip()))" >nul 2>nul
if errorlevel 1 (
  echo Installing AKShare for the first run...
  ".venv\Scripts\python.exe" -m pip install --no-cache-dir --require-hashes -r requirements.lock.txt
  if errorlevel 1 goto fail
)
if "%~1"=="" goto dashboard
".venv\Scripts\python.exe" run.py %*
if errorlevel 1 goto fail
start "" "reports\index.html"
echo Finished. Press any key to close this window.
pause >nul
exit /b 0
:dashboard
".venv\Scripts\python.exe" dashboard.py
if errorlevel 1 goto fail
exit /b 0
:fail
echo Run failed. Check the message above.
pause
exit /b 1
