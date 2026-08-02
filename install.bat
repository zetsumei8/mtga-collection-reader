@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "MIN_PYTHON=3.10"
set "PYTHON_CMD="
set "NO_PAUSE=0"
set "FORCE_CHOOSE=0"
set "LOCATION_FILE=%CD%\runtime_path.txt"
set "RUNTIME_PARENT_ARG="
set "PATCH_MODE=0"
set "PATCH_TARGET_ARG="
set "SKIP_ACTION_MENU=0"
set "PATCH_RUNTIME=0"
set "APP_VERSION=5.2.5"

:parse_args
if "%~1"=="" goto :args_done
if /I "%~1"=="--no-pause" set "NO_PAUSE=1"
if /I "%~1"=="--choose-location" set "FORCE_CHOOSE=1"
if /I "%~1"=="--runtime-parent" (
    if not "%~2"=="" set "RUNTIME_PARENT_ARG=%~2"
    shift
)
if /I "%~1"=="--patch" set "PATCH_MODE=1"
if /I "%~1"=="--patch-target" (
    if not "%~2"=="" set "PATCH_TARGET_ARG=%~2"
    shift
)
if /I "%~1"=="--skip-action-menu" set "SKIP_ACTION_MENU=1"
if /I "%~1"=="--patch-runtime" set "PATCH_RUNTIME=1"
shift
goto :parse_args

:args_done
if defined PATCH_TARGET_ARG (
    call "%CD%\patch_old_install.bat" --target "%PATCH_TARGET_ARG%" --no-pause
    exit /b !errorlevel!
)
if "%PATCH_MODE%"=="1" (
    call "%CD%\patch_old_install.bat"
    exit /b !errorlevel!
)

if "%NO_PAUSE%"=="0" if "%SKIP_ACTION_MENU%"=="0" if "%FORCE_CHOOSE%"=="0" if not defined RUNTIME_PARENT_ARG (
    echo.
    echo  MTGA Collection Reader - Setup
    echo  =================================
    echo.
    echo  [1] Install or repair this copy
    echo  [2] Patch an older installation
    echo.
    set "SETUP_ACTION="
    set /p "SETUP_ACTION=Choose an action [1]: "
    if "!SETUP_ACTION!"=="2" (
        call "%CD%\patch_old_install.bat"
        exit /b !errorlevel!
    )
)

echo.
echo  MTGA Collection Reader - Setup
echo  =================================
echo.

call :resolve_runtime_location
if errorlevel 1 goto :failed

set "VENV_DIR=%RUNTIME_ROOT%\.venv"
set "DATA_DIR=%RUNTIME_ROOT%\Data"
set "EXPORT_DIR=%RUNTIME_ROOT%\Exports"
set "PIP_CACHE_DIR=%RUNTIME_ROOT%\Package Cache"

for %%D in ("%VENV_DIR%" "%DATA_DIR%" "%EXPORT_DIR%" "%PIP_CACHE_DIR%") do (
    if not exist "%%~fD" mkdir "%%~fD" >nul 2>&1
)

if not exist "%RUNTIME_ROOT%" goto :storage_failed
>"%RUNTIME_ROOT%\.write_test.tmp" echo storage test
if not exist "%RUNTIME_ROOT%\.write_test.tmp" goto :storage_failed
del /q "%RUNTIME_ROOT%\.write_test.tmp" >nul 2>&1

>"%LOCATION_FILE%.tmp" echo(!RUNTIME_ROOT!
move /y "%LOCATION_FILE%.tmp" "%LOCATION_FILE%" >nul
if errorlevel 1 goto :storage_failed

echo App storage location:
echo   %RUNTIME_ROOT%
echo.

set "APP_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "USE_EXISTING_VENV=0"
if "%PATCH_RUNTIME%"=="1" if exist "%APP_PYTHON%" (
    "%APP_PYTHON%" -c "import platform, struct, sys; raise SystemExit(0 if sys.version_info >= (3, 10) and struct.calcsize('P') == 8 and platform.python_implementation() == 'CPython' else 1)" >nul 2>&1
    if not errorlevel 1 set "USE_EXISTING_VENV=1"
)

if "%USE_EXISTING_VENV%"=="1" (
    echo Updating the existing private app environment...
    "%APP_PYTHON%" -c "import platform, struct, sys; print('Python', sys.version.split()[0], '-', str(struct.calcsize('P') * 8) + '-bit', platform.python_implementation())"
    echo.
) else (
    call :find_python
    if not defined PYTHON_CMD (
        echo No compatible Python runtime was found.
        echo Downloading the official Python Install Manager and latest stable Python...
        echo.
        call :install_latest_python
        if errorlevel 1 goto :python_missing
        call :find_python
    )

    if not defined PYTHON_CMD goto :python_missing

    echo Using the newest compatible installed runtime:
    !PYTHON_CMD! -c "import platform, struct, sys; print('Python', sys.version.split()[0], '-', str(struct.calcsize('P') * 8) + '-bit', platform.python_implementation())"
    echo.

    echo Creating a private app environment on the selected drive...
    !PYTHON_CMD! -m venv --clear "%VENV_DIR%"
    if errorlevel 1 goto :failed
)

if not exist "%APP_PYTHON%" goto :failed

echo.
echo Updating installer tools...
"%APP_PYTHON%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 goto :failed

echo.
echo Installing or updating the small runtime dependency...
"%APP_PYTHON%" -m pip install --upgrade --prefer-binary -r "%CD%\requirements.txt"
if errorlevel 1 goto :failed

echo.
echo Verifying the complete app...
set "MTGA_READER_DATA_DIR=%DATA_DIR%"
set "MTGA_READER_OUTPUT_DIR=%EXPORT_DIR%"
"%APP_PYTHON%" -c "import requests; import collection_store, provider_contract, daemon_provider, manual_import, bridge_service; print('One-click Arena sync, manual fallback, local bridge, and SQLite components are ready.')"
if errorlevel 1 goto :failed

"%APP_PYTHON%" -m py_compile "%CD%\mtg.py" "%CD%\daemon_provider.py" "%CD%\collection_store.py" "%CD%\card_database.py" "%CD%\exports.py" "%CD%\provider_contract.py" "%CD%\manual_import.py" "%CD%\bridge_service.py"
if errorlevel 1 goto :failed

>"%CD%\installed_version.txt" echo %APP_VERSION%

echo.
echo Setup complete.
echo.
echo Runtime, helper cache, settings, and default exports are stored at:
echo   %RUNTIME_ROOT%
echo.
echo Python 3.14 and newer compatible releases are accepted; there is no upper-version gate.
echo Double-click run_reader.bat to start the app.
echo Use change_storage_drive.bat whenever you want to select another drive.
echo Use patch_old_install.bat to update a separate older installation safely.
echo.
call :maybe_pause
exit /b 0

:resolve_runtime_location
set "RUNTIME_ROOT="
if defined RUNTIME_PARENT_ARG goto :resolve_runtime_parent_arg
if exist "%LOCATION_FILE%" set /p "RUNTIME_ROOT="<"%LOCATION_FILE%"

if "%FORCE_CHOOSE%"=="1" (
    set "RUNTIME_ROOT="
    call :choose_runtime_location
    exit /b !errorlevel!
)

if defined RUNTIME_ROOT (
    if "%NO_PAUSE%"=="1" exit /b 0
    echo Current app storage location:
    echo   !RUNTIME_ROOT!
    echo.
    set "LOCATION_ACTION="
    set /p "LOCATION_ACTION=Press Enter to keep it, or type C to choose another drive: "
    if /I "!LOCATION_ACTION!"=="C" (
        set "RUNTIME_ROOT="
        call :choose_runtime_location
        exit /b !errorlevel!
    )
    exit /b 0
)

call :choose_runtime_location
exit /b !errorlevel!

:resolve_runtime_parent_arg
set "SELECTED_PARENT=!RUNTIME_PARENT_ARG!"
call :finalize_runtime_parent
exit /b !errorlevel!

:choose_runtime_location
echo Choose the drive or parent folder for the app's private runtime.
echo The installer will create an "MTGA Collection Reader Runtime" folder there.
echo.
echo Available fixed drives:
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object { $free=[math]::Round($_.FreeSpace/1GB,1); Write-Host ('  {0}  {1} GB free' -f $_.DeviceID,$free) }" 2>nul
echo.

set "PICK_FILE=%TEMP%\mtga_exporter_location_!RANDOM!_!RANDOM!.txt"
del /q "!PICK_FILE!" >nul 2>&1
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; $dialog=New-Object System.Windows.Forms.FolderBrowserDialog; $dialog.Description='Choose a drive or parent folder for MTGA Collection Reader'; $dialog.ShowNewFolderButton=$true; if($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){ [System.IO.File]::WriteAllText('!PICK_FILE!', $dialog.SelectedPath) }" >nul 2>&1

set "SELECTED_PARENT="
if exist "!PICK_FILE!" set /p "SELECTED_PARENT="<"!PICK_FILE!"
del /q "!PICK_FILE!" >nul 2>&1

if not defined SELECTED_PARENT (
    set "DEFAULT_PARENT=%~d0\"
    set /p "SELECTED_PARENT=Enter a drive letter or folder path [!DEFAULT_PARENT!]: "
    if not defined SELECTED_PARENT set "SELECTED_PARENT=!DEFAULT_PARENT!"
)

call :finalize_runtime_parent
exit /b !errorlevel!

:finalize_runtime_parent
set "SELECTED_PARENT=!SELECTED_PARENT:"=!"
if "!SELECTED_PARENT:~1,1!"=="" set "SELECTED_PARENT=!SELECTED_PARENT!:"
if "!SELECTED_PARENT:~1,1!"==":" if "!SELECTED_PARENT:~2,1!"=="" set "SELECTED_PARENT=!SELECTED_PARENT!\"

for %%I in ("!SELECTED_PARENT!") do set "SELECTED_NAME=%%~nxI"
if /I "!SELECTED_NAME!"=="MTGA Collection Reader Runtime" (
    set "RUNTIME_ROOT=!SELECTED_PARENT!"
) else (
    set "RUNTIME_ROOT=!SELECTED_PARENT!\MTGA Collection Reader Runtime"
)

for %%I in ("!RUNTIME_ROOT!") do set "RUNTIME_ROOT=%%~fI"
mkdir "!RUNTIME_ROOT!" >nul 2>&1
if not exist "!RUNTIME_ROOT!" exit /b 1

>"!RUNTIME_ROOT!\.write_test.tmp" echo storage test
if not exist "!RUNTIME_ROOT!\.write_test.tmp" exit /b 1
del /q "!RUNTIME_ROOT!\.write_test.tmp" >nul 2>&1
exit /b 0

:find_python
set "PYTHON_CMD="

where py >nul 2>&1
if not errorlevel 1 (
    call :validate_python "py -3"
    if defined PYTHON_CMD exit /b 0
)

where python >nul 2>&1
if not errorlevel 1 (
    call :validate_python "python"
    if defined PYTHON_CMD exit /b 0
)

where python3 >nul 2>&1
if not errorlevel 1 (
    call :validate_python "python3"
    if defined PYTHON_CMD exit /b 0
)

exit /b 1

:validate_python
set "CANDIDATE=%~1"
%CANDIDATE% -c "import platform, struct, sys; gil_ok = not hasattr(sys, '_is_gil_enabled') or sys._is_gil_enabled(); raise SystemExit(0 if sys.version_info >= (3, 10) and struct.calcsize('P') == 8 and platform.python_implementation() == 'CPython' and gil_ok else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PYTHON_CMD=%CANDIDATE%"
exit /b 0

:install_latest_python
call :install_default_runtime
if not errorlevel 1 exit /b 0

where winget >nul 2>&1
if not errorlevel 1 (
    echo Installing Python Install Manager through WinGet...
    winget install 9NQ7512CXL7T -e --accept-package-agreements --accept-source-agreements --disable-interactivity
    if not errorlevel 1 (
        set "PATH=%LOCALAPPDATA%\Microsoft\WindowsApps;!PATH!"
        call :install_default_runtime
        if not errorlevel 1 exit /b 0
        call :find_python
        if defined PYTHON_CMD exit /b 0
    )
)

echo WinGet installation was unavailable. Trying the official python.org App Installer...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Add-AppxPackage -AppInstallerFile 'https://www.python.org/ftp/python/pymanager/pymanager.appinstaller'"
if errorlevel 1 exit /b 1

set "PATH=%LOCALAPPDATA%\Microsoft\WindowsApps;!PATH!"
call :install_default_runtime
if not errorlevel 1 exit /b 0
call :find_python
if defined PYTHON_CMD exit /b 0
exit /b 1

:install_default_runtime
where pymanager >nul 2>&1
if not errorlevel 1 (
    pymanager install default
    if errorlevel 1 exit /b 1
    exit /b 0
)

if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\pymanager.exe" (
    "%LOCALAPPDATA%\Microsoft\WindowsApps\pymanager.exe" install default
    if errorlevel 1 exit /b 1
    exit /b 0
)

for /d %%D in ("%LOCALAPPDATA%\Microsoft\WindowsApps\PythonSoftwareFoundation.PythonManager_*") do (
    if exist "%%~fD\pymanager.exe" (
        "%%~fD\pymanager.exe" install default
        if not errorlevel 1 exit /b 0
    )
)

exit /b 1

:storage_failed
echo.
echo The selected storage location could not be created or written to:
echo   %RUNTIME_ROOT%
echo Choose another drive or run setup with permission to write there.
echo.
call :maybe_pause
exit /b 1

:python_missing
echo.
echo Setup could not install or locate a standard 64-bit CPython %MIN_PYTHON% or newer runtime.
echo Check your internet connection and Windows App Installer or WinGet permissions.
echo You may also install the current stable 64-bit Python from python.org, then run setup again.
echo.
call :maybe_pause
exit /b 1

:failed
echo.
echo Setup failed. Review the error above, then run this file again.
echo The selected runtime folder can be deleted safely before retrying.
echo.
call :maybe_pause
exit /b 1

:maybe_pause
if "%NO_PAUSE%"=="0" pause
exit /b 0
