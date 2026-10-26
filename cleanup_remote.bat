@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ========================================
echo   Ark Files API - usage check / cleanup
echo ========================================
echo.
echo Files expire automatically after 7 days.
echo Total capacity is 20 GB - cleanup is rarely needed.
echo Do NOT run while the bot is analyzing a file.
echo.
echo   [s] show usage only
echo   [y] clean up all files
echo   any other key = cancel
echo.
set /p CONFIRM=Choice [s/y]:
if /i "%CONFIRM%"=="s" (
    echo.
    if exist "venv\Scripts\python.exe" (
        "venv\Scripts\python.exe" "cleanup_remote.py" --stats
    ) else (
        python "cleanup_remote.py" --stats
    )
    echo.
    pause
    exit /b 0
)
if /i not "%CONFIRM%"=="y" (
    echo Cancelled.
    pause
    exit /b 0
)

echo.
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" "cleanup_remote.py"
) else (
    python "cleanup_remote.py"
)

echo.
pause
endlocal
