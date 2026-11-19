@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   PhotoCraft Generative Fill
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3.10+ and try again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -c "import customtkinter, PIL, dotenv, google.genai" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Default PyPI download failed. Retrying via mirror...
        ".venv\Scripts\python.exe" -m pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
        if errorlevel 1 (
            echo.
            echo Dependency install failed. Check your network or proxy, then rerun run.bat.
            pause
            exit /b 1
        )
    )
)

if not exist ".tmp" mkdir ".tmp"

echo Starting the floating toolbar...
echo.
echo   PhotoCraft must be running with its control server enabled:
echo     photocraft.exe --control 8765 --control-token ^<64-hex^>
echo   and its automation read root must include:
echo     %CD%
echo.
echo   The token and port live in Settings (gear icon) inside the app.
echo.

".venv\Scripts\python.exe" ui.py
if errorlevel 1 pause
endlocal
