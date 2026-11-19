@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo   PhotoCraft Generative Fill - Installer
echo ============================================================
echo.

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "PHOTO_DIR=%APP_DIR%\..\PhotoCraft"
for %%I in ("%PHOTO_DIR%") do set "PHOTO_DIR=%%~fI"

set "CORE_EXE=%PHOTO_DIR%\photocraft_core.exe"
set "LAUNCHER_EXE=%PHOTO_DIR%\photocraft.exe"
set "ORIGINAL_EXE=%PHOTO_DIR%\photocraft_original.exe"

echo   App folder   : %APP_DIR%
echo   PhotoCraft   : %PHOTO_DIR%
echo.

if not exist "%PHOTO_DIR%" (
    echo [ERROR] PhotoCraft folder not found: %PHOTO_DIR%
    pause
    exit /b 1
)

REM ---------------------------------------------------------------- 1. backup
echo [1/5] Preparing the PhotoCraft binary...
if exist "%CORE_EXE%" (
    echo       Already installed: photocraft_core.exe exists.
    echo       Skipping the rename so the original is never overwritten.
) else (
    if not exist "%LAUNCHER_EXE%" (
        echo [ERROR] Neither photocraft.exe nor photocraft_core.exe found in:
        echo         %PHOTO_DIR%
        pause
        exit /b 1
    )
    echo       Backing up photocraft.exe -^> photocraft_original.exe
    if not exist "%ORIGINAL_EXE%" (
        copy /Y "%LAUNCHER_EXE%" "%ORIGINAL_EXE%" >nul
        if errorlevel 1 (
            echo [ERROR] Could not create the backup copy.
            pause
            exit /b 1
        )
    )
    echo       Renaming photocraft.exe -^> photocraft_core.exe
    move /Y "%LAUNCHER_EXE%" "%CORE_EXE%" >nul
    if errorlevel 1 (
        echo [ERROR] Could not rename the binary. Is PhotoCraft still running?
        echo         Close PhotoCraft and run install.bat again.
        pause
        exit /b 1
    )
)

REM ----------------------------------------------------------------- 2. venv
echo [2/5] Checking the Python environment...
if not exist "%APP_DIR%\.venv\Scripts\python.exe" (
    echo       Creating virtual environment...
    where python >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python not found on PATH. Install Python 3.10+ first.
        pause
        exit /b 1
    )
    python -m venv "%APP_DIR%\.venv"
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
)

set "PY=%APP_DIR%\.venv\Scripts\python.exe"
set "PYW=%APP_DIR%\.venv\Scripts\pythonw.exe"

echo       Installing runtime dependencies...
"%PY%" -m pip install --upgrade pip >nul 2>nul
"%PY%" -m pip install -r "%APP_DIR%\requirements.txt"
if errorlevel 1 (
    echo       Default index failed, retrying via mirror...
    "%PY%" -m pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r "%APP_DIR%\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Could not install dependencies.
        pause
        exit /b 1
    )
)

REM ------------------------------------------------------------ 3. pyinstaller
echo [3/5] Installing PyInstaller...
"%PY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    "%PY%" -m pip install pyinstaller
    if errorlevel 1 (
        echo       Default index failed, retrying via mirror...
        "%PY%" -m pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller
        if errorlevel 1 (
            echo [ERROR] Could not install PyInstaller.
            pause
            exit /b 1
        )
    )
)

REM ----------------------------------------------------------------- 4. build
echo [4/5] Building the launcher...
if exist "%APP_DIR%\.tmp" rmdir /S /Q "%APP_DIR%\.tmp" >nul 2>nul
mkdir "%APP_DIR%\.tmp" >nul 2>nul

if exist "%APP_DIR%\build" rmdir /S /Q "%APP_DIR%\build" >nul 2>nul
if exist "%APP_DIR%\dist" rmdir /S /Q "%APP_DIR%\dist" >nul 2>nul

set "ICON_ARG="
if exist "%APP_DIR%\app.ico" set "ICON_ARG=--icon %APP_DIR%\app.ico"

"%PY%" -m PyInstaller --onefile --noconsole --name photocraft --distpath "%APP_DIR%\dist" --workpath "%APP_DIR%\build" --specpath "%APP_DIR%\build" %ICON_ARG% "%APP_DIR%\launcher.py"
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    pause
    exit /b 1
)

if not exist "%APP_DIR%\dist\photocraft.exe" (
    echo [ERROR] Build produced no executable.
    pause
    exit /b 1
)

echo       Installing launcher -^> %LAUNCHER_EXE%
copy /Y "%APP_DIR%\dist\photocraft.exe" "%LAUNCHER_EXE%" >nul
if errorlevel 1 (
    echo [ERROR] Could not copy the launcher into the PhotoCraft folder.
    pause
    exit /b 1
)

REM --------------------------------------------------------------- 5. verify
echo [5/5] Verifying...
"%PY%" "%APP_DIR%\install_verify.py"
if errorlevel 1 (
    echo.
    echo [WARN] Verification reported a problem. See the messages above.
)

echo.
echo ============================================================
echo   Done.
echo.
echo   Launch PhotoCraft as you normally would - Desktop shortcut,
echo   Start Menu, or by opening an image file. The Generative Fill
echo   toolbar starts with it and closes when PhotoCraft closes.
echo.
echo   To undo everything: run uninstall.bat
echo ============================================================
echo.
pause
endlocal
