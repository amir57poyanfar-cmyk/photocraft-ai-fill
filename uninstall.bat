@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo   PhotoCraft Generative Fill - Uninstaller
echo ============================================================
echo.

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "PHOTO_DIR=%APP_DIR%\..\PhotoCraft"
for %%I in ("%PHOTO_DIR%") do set "PHOTO_DIR=%%~fI"

set "CORE_EXE=%PHOTO_DIR%\photocraft_core.exe"
set "LAUNCHER_EXE=%PHOTO_DIR%\photocraft.exe"
set "ORIGINAL_EXE=%PHOTO_DIR%\photocraft_original.exe"

echo   PhotoCraft : %PHOTO_DIR%
echo.

if not exist "%PHOTO_DIR%" (
    echo [ERROR] PhotoCraft folder not found: %PHOTO_DIR%
    pause
    exit /b 1
)

REM ------------------------------------------------------------- 1. stop apps
echo [1/3] Closing PhotoCraft if it is running...
tasklist /FI "IMAGENAME eq photocraft_core.exe" 2>nul | find /I "photocraft_core.exe" >nul
if not errorlevel 1 (
    echo       Stopping photocraft_core.exe...
    taskkill /IM photocraft_core.exe /F >nul 2>nul
    timeout /t 2 /nobreak >nul
)
tasklist /FI "IMAGENAME eq photocraft.exe" 2>nul | find /I "photocraft.exe" >nul
if not errorlevel 1 (
    echo       Stopping photocraft.exe...
    taskkill /IM photocraft.exe /F >nul 2>nul
    timeout /t 2 /nobreak >nul
)

REM --------------------------------------------------------------- 2. restore
echo [2/3] Restoring the original binary...
if exist "%CORE_EXE%" (
    if exist "%LAUNCHER_EXE%" (
        echo       Removing the installed launcher...
        del /F /Q "%LAUNCHER_EXE%" >nul 2>nul
        if exist "%LAUNCHER_EXE%" (
            echo [ERROR] Could not delete %LAUNCHER_EXE%
            echo         Close PhotoCraft and try again.
            pause
            exit /b 1
        )
    )
    echo       Renaming photocraft_core.exe -^> photocraft.exe
    move /Y "%CORE_EXE%" "%LAUNCHER_EXE%" >nul
    if errorlevel 1 (
        echo [ERROR] Could not restore the original binary.
        pause
        exit /b 1
    )
) else (
    echo       No photocraft_core.exe present - nothing to restore.
)

if exist "%ORIGINAL_EXE%" (
    echo       Removing the safety backup photocraft_original.exe
    del /F /Q "%ORIGINAL_EXE%" >nul 2>nul
)

REM --------------------------------------------------------------- 3. cleanup
echo [3/3] Cleaning build artifacts...
if exist "%APP_DIR%\build" rmdir /S /Q "%APP_DIR%\build" >nul 2>nul
if exist "%APP_DIR%\dist" rmdir /S /Q "%APP_DIR%\dist" >nul 2>nul
if exist "%APP_DIR%\photocraft.spec" del /F /Q "%APP_DIR%\photocraft.spec" >nul 2>nul
if exist "%APP_DIR%\.tmp" rmdir /S /Q "%APP_DIR%\.tmp" >nul 2>nul

echo.
echo ============================================================
echo   Uninstalled.
echo.
echo   PhotoCraft is back to a normal single executable.
echo   Your settings in config.json and the .venv were kept.
echo ============================================================
echo.
pause
endlocal
