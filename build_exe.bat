@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "LOCATION_FILE=%CD%\runtime_path.txt"
set "RUNTIME_ROOT="
if exist "%LOCATION_FILE%" set /p "RUNTIME_ROOT="<"%LOCATION_FILE%"
if not defined RUNTIME_ROOT goto :prepare
set "APP_PYTHON=%RUNTIME_ROOT%\.venv\Scripts\python.exe"
goto :start

:prepare
echo Preparing the app environment first...
call install.bat --no-pause
if errorlevel 1 goto :failed
set "RUNTIME_ROOT="
if exist "%LOCATION_FILE%" set /p "RUNTIME_ROOT="<"%LOCATION_FILE%"
if not defined RUNTIME_ROOT goto :failed
set "APP_PYTHON=%RUNTIME_ROOT%\.venv\Scripts\python.exe"

:start
echo.
echo  Building standalone MTGA Collection Reader EXE
echo  =====================================
echo.

if not exist "%APP_PYTHON%" (
    echo Preparing the app environment first...
    call install.bat --no-pause
    if errorlevel 1 goto :failed
)

set "PIP_CACHE_DIR=%RUNTIME_ROOT%\Package Cache"
"%APP_PYTHON%" -m pip install --upgrade pyinstaller
if errorlevel 1 goto :failed

"%APP_PYTHON%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name MTGA_Collection_Reader ^
  --hidden-import=requests ^
  mtg.py
if errorlevel 1 goto :failed

echo.
echo Build complete.
echo Standalone app: dist\MTGA_Collection_Reader.exe
echo The standalone EXE includes one-click Arena sync, SQLite, manual fallback, and the localhost bridge.
echo.
pause
exit /b 0

:failed
echo.
echo Build failed. Review the error above.
echo.
pause
exit /b 1
