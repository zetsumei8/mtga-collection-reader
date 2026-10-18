@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LOCATION_FILE=%CD%\runtime_path.txt"
set "RUNTIME_ROOT="
if exist "%LOCATION_FILE%" set /p "RUNTIME_ROOT="<"%LOCATION_FILE%"
if not defined RUNTIME_ROOT goto :repair

set "APP_PYTHON=%RUNTIME_ROOT%\.venv\Scripts\python.exe"
set "APP_PYTHONW=%RUNTIME_ROOT%\.venv\Scripts\pythonw.exe"
set "MTGA_READER_DATA_DIR=%RUNTIME_ROOT%\Data"
set "MTGA_READER_OUTPUT_DIR=%RUNTIME_ROOT%\Exports"
set "PIP_CACHE_DIR=%RUNTIME_ROOT%\Package Cache"
set "CHECK=import requests, PySide6.QtWebEngineWidgets, PySide6.QtWebChannel, collection_store, provider_contract, daemon_provider, manual_import, bridge_service, web_ui"

if not exist "%APP_PYTHON%" goto :repair
if not exist "%APP_PYTHONW%" goto :repair

"%APP_PYTHON%" -c "%CHECK%" >nul 2>&1
if errorlevel 1 goto :repair

goto :launch

:repair
echo.
echo App setup is missing or incomplete. Repairing it now...
echo.
call install.bat --no-pause
if errorlevel 1 goto :missing

set "RUNTIME_ROOT="
if exist "%LOCATION_FILE%" set /p "RUNTIME_ROOT="<"%LOCATION_FILE%"
if not defined RUNTIME_ROOT goto :missing
set "APP_PYTHON=%RUNTIME_ROOT%\.venv\Scripts\python.exe"
set "APP_PYTHONW=%RUNTIME_ROOT%\.venv\Scripts\pythonw.exe"
set "MTGA_READER_DATA_DIR=%RUNTIME_ROOT%\Data"
set "MTGA_READER_OUTPUT_DIR=%RUNTIME_ROOT%\Exports"
set "PIP_CACHE_DIR=%RUNTIME_ROOT%\Package Cache"

if not exist "%APP_PYTHONW%" goto :missing
"%APP_PYTHON%" -c "%CHECK%" >nul 2>&1
if errorlevel 1 goto :missing

:launch
start "" /D "%CD%" "%APP_PYTHONW%" "%CD%\mtg.py"
exit /b 0

:missing
echo.
echo Python or required packages could not be prepared.
echo Run install.bat and review the reported error.
echo.
pause
exit /b 1
