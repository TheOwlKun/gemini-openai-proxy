@echo off
REM ============================================================
REM  Gemini OpenAI Proxy — Windows Start Script
REM ============================================================
REM
REM  Usage:
REM    start.bat              - Interactive launcher
REM    start.bat foreground   - Run in foreground
REM    start.bat background   - Run in background
REM    start.bat stop         - Stop background server
REM
REM ============================================================

title Gemini OpenAI Proxy Server

set SCRIPT_DIR=%~dp0
set SERVER_SCRIPT=%SCRIPT_DIR%openai_server.py
set PID_FILE=%SCRIPT_DIR%server.pid
set LOG_FILE=%SCRIPT_DIR%server.log

echo.
echo  ============================================================
echo    Gemini OpenAI Proxy Server (Windows)
echo  ============================================================
echo.

if "%1"=="foreground" goto :foreground
if "%1"=="background" goto :background
if "%1"=="stop" goto :stop

echo   1) Foreground       - Run here, Ctrl+C to stop
echo   2) Background       - Run hidden (survives terminal close)
echo   3) Stop             - Stop background server
echo   0) Exit
echo.

set /p choice="  Choose [1-3]: "

if "%choice%"=="1" goto :foreground
if "%choice%"=="2" goto :background
if "%choice%"=="3" goto :stop
if "%choice%"=="0" goto :eof

echo  Invalid choice.
goto :eof

:foreground
echo.
echo  Starting server in foreground...
echo  Press Ctrl+C to stop
echo.
cd /d "%SCRIPT_DIR%"
python "%SERVER_SCRIPT%"
goto :eof

:background
echo.
echo  Starting server in background...
cd /d "%SCRIPT_DIR%"

REM Use pythonw if available, otherwise use START /B
where pythonw >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    start "" /B pythonw "%SERVER_SCRIPT%" > "%LOG_FILE%" 2>&1
) else (
    start "" /B python "%SERVER_SCRIPT%" > "%LOG_FILE%" 2>&1
)

echo.
echo  Server started in background!
echo   Logs: %LOG_FILE%
echo   Stop: start.bat stop
echo.
goto :eof

:stop
echo.
echo  Stopping server...
REM Try to find and kill python running openai_server
for /f "tokens=2" %%i in ('tasklist /fi "imagename eq python.exe" /fo table /nh 2^>nul') do (
    wmic process where "processid=%%i" get commandline 2>nul | findstr /i "openai_server" >nul 2>&1
    if not errorlevel 1 (
        taskkill /f /pid %%i >nul 2>&1
        echo  Stopped process %%i
    )
)
for /f "tokens=2" %%i in ('tasklist /fi "imagename eq pythonw.exe" /fo table /nh 2^>nul') do (
    wmic process where "processid=%%i" get commandline 2>nul | findstr /i "openai_server" >nul 2>&1
    if not errorlevel 1 (
        taskkill /f /pid %%i >nul 2>&1
        echo  Stopped process %%i
    )
)
echo  Done.
goto :eof
