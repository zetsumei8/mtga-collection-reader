@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo  Change MTGA Collection Reader Storage Drive
echo  ==================================
echo.
echo This creates a fresh private runtime on the new drive.
echo Your old runtime is not deleted automatically.
echo.
call install.bat --choose-location
exit /b %errorlevel%
