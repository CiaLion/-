@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ---- green/portable environment (relative paths only) ----
set "PYTHONNOUSERSITE=1"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0;%~dp0.venv\Lib\site-packages"
set "QT_PLUGIN_PATH=%~dp0.venv\Lib\site-packages\PyQt5\Qt5\plugins"
set "QT_QPA_PLATFORM_PLUGIN_PATH=%~dp0.venv\Lib\site-packages\PyQt5\Qt5\plugins\platforms"

rem ---- choose portable runtime first, fall back to .venv ----
set "PYW=%~dp0runtime\python311\pythonw.exe"
set "PYC=%~dp0runtime\python311\python.exe"
if not exist "%PYW%" set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYC%" set "PYC=%~dp0.venv\Scripts\python.exe"

rem ---- smoke test: no elevation, no downloads, console output ----
if /I "%~1"=="--smoke" goto smoke

rem ---- require admin (SendInput into the game) ----
fltmc >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%~dp0' -Verb RunAs"
    exit /b
)
cd /d "%~dp0"

rem ---- heal pyvenv.cfg for subprocesses (silent) ----
if exist "%~dp0setup\heal_venv.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0setup\heal_venv.ps1" >nul 2>&1
)

rem ---- first run: only the main window environment is prepared here ----
rem  The AI transcription stack (vendors + 2.6 GB weights) is installed from
rem  inside the app: "Environment check -> one-click install".
set "NEED=0"
if not exist "%PYC%" set "NEED=1"
if not exist "%~dp0.venv\Lib\site-packages\PyQt5" set "NEED=1"
if "%NEED%"=="1" (
    echo.
    echo ============================================================
    echo  First run: preparing the main Python environment.
    echo  This can take a while; keep the window open.
    echo ============================================================
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\bootstrap.ps1"
    if errorlevel 1 (
        echo.
        echo [FAIL] Setup failed. See the message above, then run:
        echo        the one-click setup bat in the setup folder.
        pause
        exit /b 1
    )
)

if not exist "%PYW%" (
    echo [FAIL] portable pythonw.exe not found. Run setup first.
    pause
    exit /b 1
)

start "" /d "%~dp0" /wait "%PYW%" "%~dp0main.py" %*
if errorlevel 1 (
    echo.
    echo [ERROR] GUI exited with error code %errorlevel%.
    if exist "%~dp0logs\main_error.log" type "%~dp0logs\main_error.log"
    if exist "%PYC%" "%PYC%" "%~dp0main.py"
    pause
)
exit /b 0

:smoke
if not exist "%PYC%" (
    echo [FAIL] python.exe not found.
    exit /b 1
)
"%PYC%" "%~dp0main.py" %*
exit /b %errorlevel%
