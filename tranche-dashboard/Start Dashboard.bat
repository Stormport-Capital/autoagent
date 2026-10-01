@echo off
title Tranche Dashboard
cd /d "%~dp0"
echo ============================================================
echo  Tranche Dashboard
echo  Keep this window open while it runs. Close it to stop.
echo ============================================================
echo.

rem -- find Python ------------------------------------------------
set "PY="
python --version >nul 2>nul && set "PY=python"
if not defined PY (
  py --version >nul 2>nul && set "PY=py"
)
if not defined PY goto nopython

rem -- get the latest version (skipped quietly if offline) --------
where git >nul 2>nul
if not errorlevel 1 (
  git pull --ff-only -q >nul 2>nul
  if errorlevel 1 echo Could not check for updates - starting the current version.
)

rem -- install or refresh the two small dependencies ----------------
%PY% -m pip install -q --disable-pip-version-check -r requirements.txt

rem -- first run: create .env and open it for the keys --------------
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo A settings file for your keys was created and opened in Notepad.
  echo Paste each key after its = sign, then File - Save, then close Notepad.
  notepad ".env"
)

%PY% app.py --open
echo.
echo The dashboard has stopped.
pause
exit /b 0

:nopython
echo Python was not found.
echo Install it from https://www.python.org/downloads/
echo and tick "Add Python to PATH" in the installer, then double-click this file again.
pause
exit /b 1
