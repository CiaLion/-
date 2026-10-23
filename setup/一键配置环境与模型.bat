@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"

echo ============================================================
echo  YiJian YanZou - one-click setup (idempotent)
echo ============================================================
echo  Logs: logs\setup_env.log  and  logs\fetch_models.log
echo.

echo [1/3] Python environments (empty/incomplete venvs are repaired)...
powershell -NoProfile -ExecutionPolicy Bypass -File "setup\setup_env.ps1"
if errorlevel 1 goto fail

echo.
echo [2/3] Model weights...
if exist "vendor\sheetsage2\models\SheetSage2\model.safetensors" if exist "vendor\sheetsage2\models\MERT-v2-FullSong\model.safetensors" goto envok
echo [..] weights missing, downloading (about 2.6 GB, no login needed)...
powershell -NoProfile -ExecutionPolicy Bypass -File "setup\fetch_models.ps1"
if errorlevel 1 goto fail

:envok
if exist "setup\heal_venv.ps1" powershell -NoProfile -ExecutionPolicy Bypass -File "setup\heal_venv.ps1" >nul 2>&1

echo.
echo [3/3] Verifying environment...
powershell -NoProfile -ExecutionPolicy Bypass -File "setup\setup_env.ps1" -Check
if errorlevel 1 goto fail

echo.
echo Done. Double-click the launcher in this folder to start.
pause
exit /b 0

:fail
echo.
echo [FAIL] One-click setup did not finish.
echo        Read logs\setup_env.log / logs\fetch_models.log and the messages above.
echo        You can rerun this script safely; finished downloads are kept.
pause
exit /b 1
