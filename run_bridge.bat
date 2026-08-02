@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "runtime_path.txt" (
  echo Runtime path not found. Run install.bat first.
  pause
  exit /b 1
)
set /p RUNTIME_ROOT=<"runtime_path.txt"
if not exist "%RUNTIME_ROOT%\.venv\Scripts\python.exe" (
  echo Private Python environment not found. Run install.bat first.
  pause
  exit /b 1
)
set "MTGA_READER_DATA_DIR=%RUNTIME_ROOT%\Data"
"%RUNTIME_ROOT%\.venv\Scripts\python.exe" "%~dp0bridge_service.py" --data-dir "%MTGA_READER_DATA_DIR%"
if errorlevel 1 pause
