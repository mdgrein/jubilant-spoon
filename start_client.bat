@echo off

set VENV_DIR=%~dp0.venv
set REQUIREMENTS=%~dp0requirements.txt

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo Installing requirements...
"%VENV_DIR%\Scripts\pip" install -q -r "%REQUIREMENTS%"
if errorlevel 1 (
    echo Failed to install requirements.
    pause
    exit /b 1
)

echo Starting Clowder client...
"%VENV_DIR%\Scripts\python" -m client.main
