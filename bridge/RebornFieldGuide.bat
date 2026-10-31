@echo off
rem Reborn Field Guide - double-click to start the guide and open it in your browser.
rem Any arguments are passed on to run.py, e.g.  RebornFieldGuide.bat --game "C:\Games\Pokemon Reborn"
setlocal EnableExtensions
cd /d "%~dp0"
title Reborn Field Guide

set "PY="
call :try py -3
if not defined PY call :try python
if not defined PY call :try python3
if not defined PY goto :nopython

%PY% "%~dp0run.py" %*
if errorlevel 1 (
  echo.
  echo The guide stopped with an error. See the messages above.
  pause
)
exit /b

:try
rem Checks that the command is a real Python 3.9+ (the Microsoft Store stub "python" fails this).
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=%*"
exit /b

:nopython
echo.
echo  Python 3 was not found on this computer.
echo.
echo  1. Download Python 3 from  https://www.python.org/downloads/
echo  2. Run the installer and tick  "Add python.exe to PATH"  on the first screen.
echo  3. Double-click RebornFieldGuide.bat again.
echo.
pause
exit /b 1
