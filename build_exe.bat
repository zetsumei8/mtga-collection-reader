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
set "BUILD_ENV=%RUNTIME_ROOT%\.build-venv-py313"
set "BUILD_PYTHON=%BUILD_ENV%\Scripts\python.exe"

py -3.13 -c "import platform, struct, sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) and struct.calcsize('P') == 8 and platform.python_implementation() == 'CPython' else 1)" >nul 2>&1
if errorlevel 1 goto :build_python_missing

if not exist "%BUILD_PYTHON%" (
  echo Preparing the isolated Python 3.13 Qt WebEngine build environment...
  py -3.13 -m venv "%BUILD_ENV%"
  if errorlevel 1 goto :build_python_missing
)

echo Installing or updating the verified Python 3.13 build dependencies...
"%BUILD_PYTHON%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :failed
"%BUILD_PYTHON%" -m pip install --prefer-binary --upgrade -r "%CD%\build_requirements.txt"
if errorlevel 1 goto :failed

"%BUILD_PYTHON%" -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --distpath "%RUNTIME_ROOT%\Builds" ^
  --workpath "%BUILD_ENV%\PyInstaller Build" ^
  --name MTGA_Collection_Reader ^
  --hidden-import=requests ^
  --add-data "ui;ui" ^
  mtg.py
if errorlevel 1 goto :failed

echo.
echo Build complete.
echo App folder: %RUNTIME_ROOT%\Builds\MTGA_Collection_Reader\
echo Launch: %RUNTIME_ROOT%\Builds\MTGA_Collection_Reader\MTGA_Collection_Reader.exe
echo Keep the app folder together; it includes Qt WebEngine and the local HTML/CSS interface.
echo.
pause
exit /b 0

:failed
echo.
echo Build failed. Review the error above.
echo.
pause
exit /b 1

:build_python_missing
echo.
echo Python 3.13 64-bit is required for a stable frozen Qt WebEngine build.
echo The app runtime can use another supported Python version; install Python 3.13 and try again.
echo.
pause
exit /b 1
