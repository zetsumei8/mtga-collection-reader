@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

set "TARGET_PATH="
set "NO_PAUSE=0"
set "FORCE_PATCH=0"

:parse_args
if "%~1"=="" goto :run
if /I "%~1"=="--target" (
    if not "%~2"=="" set "TARGET_PATH=%~2"
    shift
) else if /I "%~1"=="--no-pause" (
    set "NO_PAUSE=1"
) else if /I "%~1"=="--force" (
    set "FORCE_PATCH=1"
)
shift
goto :parse_args

:run
if not exist "%~dp0patch_old_install.ps1" (
    echo Patch engine is missing: patch_old_install.ps1
    if "%NO_PAUSE%"=="0" pause
    exit /b 1
)

if defined TARGET_PATH (
    if "%NO_PAUSE%"=="1" (
        if "%FORCE_PATCH%"=="1" (
            powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0patch_old_install.ps1" -TargetPath "%TARGET_PATH%" -NoPause -ForcePatch
        ) else (
            powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0patch_old_install.ps1" -TargetPath "%TARGET_PATH%" -NoPause
        )
    ) else (
        if "%FORCE_PATCH%"=="1" (
            powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0patch_old_install.ps1" -TargetPath "%TARGET_PATH%" -ForcePatch
        ) else (
            powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0patch_old_install.ps1" -TargetPath "%TARGET_PATH%"
        )
    )
) else (
    if "%NO_PAUSE%"=="1" (
        if "%FORCE_PATCH%"=="1" (
            powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0patch_old_install.ps1" -NoPause -ForcePatch
        ) else (
            powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0patch_old_install.ps1" -NoPause
        )
    ) else (
        if "%FORCE_PATCH%"=="1" (
            powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0patch_old_install.ps1" -ForcePatch
        ) else (
            powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0patch_old_install.ps1"
        )
    )
)

exit /b %errorlevel%
