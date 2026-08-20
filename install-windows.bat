@echo off
setlocal EnableExtensions
title ytarchive Library Setup

set "APP_NAME=ytarchive Library"
set "SCRIPT_DIR=%~dp0"
if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "INSTALL_DIR=%LOCALAPPDATA%\ytarchive-lib\app"
set "APP_PYTHON=%INSTALL_DIR%\Scripts\python.exe"
set "APP_GUI=%INSTALL_DIR%\Scripts\pythonw.exe"
set "APP_LAUNCHER=%INSTALL_DIR%\Scripts\ytarchive-lib.exe"
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links" set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
if exist "%ProgramFiles%\MPV Player" set "PATH=%ProgramFiles%\MPV Player;%PATH%"

echo ytarchive Library setup
echo.
echo This installs the app for your Windows account and creates Desktop and Start Menu shortcuts.
echo Your music and settings are not changed by setup.
echo.

call :find_python

set "NEED_MPV="
set "NEED_FFMPEG="
set "NEED_DENO="
where mpv.exe >nul 2>nul || set "NEED_MPV=1"
where ffmpeg.exe >nul 2>nul || set "NEED_FFMPEG=1"
where ffprobe.exe >nul 2>nul || set "NEED_FFMPEG=1"
where deno.exe >nul 2>nul || set "NEED_DENO=1"

if defined PYTHON_COMMAND if not defined NEED_MPV if not defined NEED_FFMPEG if not defined NEED_DENO goto install_app

echo Some required tools are missing. Setup can install them with Windows Package Manager:
if not defined PYTHON_COMMAND echo   - Python 3.13
if defined NEED_MPV echo   - mpv ^(shinchiro Windows build^)
if defined NEED_FFMPEG echo   - FFmpeg ^(Gyan Windows build^)
if defined NEED_DENO echo   - Deno
echo.
set "INSTALL_TOOLS=Y"
set /p "INSTALL_TOOLS=Install the missing tools now? [Y/n] "
if /I "%INSTALL_TOOLS%"=="N" goto prerequisites_skipped
if /I "%INSTALL_TOOLS%"=="NO" goto prerequisites_skipped

where winget.exe >nul 2>nul
if errorlevel 1 (
    echo.
    echo Windows Package Manager was not found. Install the missing tools using the links in README.md,
    echo then run this setup file again.
    goto prerequisites_skipped
)

if not defined PYTHON_COMMAND call :winget_install Python.Python.3.13
if defined NEED_MPV call :winget_install shinchiro.mpv
if defined NEED_FFMPEG call :winget_install Gyan.FFmpeg
if defined NEED_DENO call :winget_install DenoLand.Deno

call :refresh_path
call :find_python

:prerequisites_skipped
if not defined PYTHON_COMMAND (
    echo.
    echo Python 3.10 or newer is required before the app can be installed.
    echo Download it from https://www.python.org/downloads/windows/ and run setup again.
    goto failed
)

:install_app
echo.
echo Installing %APP_NAME%...
if exist "%APP_PYTHON%" goto install_package
call %PYTHON_COMMAND% -m venv "%INSTALL_DIR%"
if errorlevel 1 (
    echo Python could not create the private app environment.
    goto failed
)

:install_package
"%APP_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
    echo The Python installer could not be updated. Check your internet connection.
    goto failed
)

rem Use an explicit current-directory suffix so a source path ending in a
rem backslash cannot leave a trailing quote in the argument passed to pip.
set "PACKAGE_SOURCE=%SCRIPT_DIR%."
for %%F in ("%SCRIPT_DIR%ytarchive_lib-*.whl") do if exist "%%~fF" set "PACKAGE_SOURCE=%%~fF"

"%APP_PYTHON%" -m pip install --upgrade "%PACKAGE_SOURCE%"
if errorlevel 1 (
    echo The app could not be installed. Check your internet connection and run setup again.
    goto failed
)

"%APP_LAUNCHER%" shortcuts
if errorlevel 1 (
    echo The app was installed, but its shortcuts could not be created.
    echo You can still start it with:
    echo   "%APP_LAUNCHER%"
)

echo.
echo Checking the tools used for playback and downloads...
"%APP_LAUNCHER%" doctor
if errorlevel 1 (
    echo.
    echo The app is installed, but one or more required tools are still missing.
    echo Install the items listed above, then open %APP_NAME% again.
)

echo.
echo %APP_NAME% is installed.
echo Use the Desktop or Start Menu shortcut to open it.
echo To update later, download a newer setup bundle and run this file again.
echo.
set "START_APP=Y"
set /p "START_APP=Start %APP_NAME% now? [Y/n] "
if /I not "%START_APP%"=="N" if /I not "%START_APP%"=="NO" (
    if exist "%APP_GUI%" (
        start "" "%APP_GUI%" -m ytarchive
    ) else (
        start "" "%APP_LAUNCHER%"
    )
)
exit /b 0

:find_python
set "PYTHON_COMMAND="
where py.exe >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info ^>= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_COMMAND=py -3"
)
if defined PYTHON_COMMAND exit /b 0
where python.exe >nul 2>nul
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info ^>= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_COMMAND=python"
)
if defined PYTHON_COMMAND exit /b 0
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PYTHON_COMMAND=^"%LOCALAPPDATA%\Programs\Python\Python313\python.exe^""
)
exit /b 0

:winget_install
echo.
echo Installing %~1...
winget install --id "%~1" --exact --source winget --accept-package-agreements --accept-source-agreements
if errorlevel 1 echo Windows Package Manager could not install %~1. You can retry it later.
exit /b 0

:refresh_path
for /f "usebackq delims=" %%P in (`powershell.exe -NoLogo -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%%P"
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links" set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
if exist "%ProgramFiles%\MPV Player" set "PATH=%ProgramFiles%\MPV Player;%PATH%"
exit /b 0

:failed
echo.
echo Setup could not finish. The message above explains what went wrong.
echo You can correct the problem and run this file again.
echo.
pause
exit /b 1
