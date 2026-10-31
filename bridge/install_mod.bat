@echo off
rem Copies the live-link mod into your Pokemon Reborn folder (Data\Mods\RebornFieldGuideLink.rb).
rem Usage: double-click and paste/drag the game folder, or drop the game folder onto this file,
rem        or run:  install_mod.bat "C:\Games\Pokemon Reborn"
rem It only adds this one file; it does not touch your save files.
setlocal EnableExtensions
set "SRC=%~dp0mod\RebornFieldGuideLink.rb"
if not exist "%SRC%" (
  echo Cannot find "%SRC%"
  goto :fail
)

set "GAME=%~1"
if not "%GAME%"=="" goto :have
echo Drag your Pokemon Reborn folder (the one with Game.exe) into this window and press Enter.
set /p "GAME=Reborn folder: "
:have
if "%GAME%"=="" goto :fail
set "GAME=%GAME:"=%"
if "%GAME:~-1%"=="\" set "GAME=%GAME:~0,-1%"

if not exist "%GAME%\Data\" (
  echo.
  echo "%GAME%" does not look like the Reborn game folder: it has no Data folder.
  goto :fail
)
if not exist "%GAME%\Data\Mods\" mkdir "%GAME%\Data\Mods"
if errorlevel 1 goto :fail
copy /y "%SRC%" "%GAME%\Data\Mods\RebornFieldGuideLink.rb" >nul
if errorlevel 1 goto :fail

echo.
echo Installed: %GAME%\Data\Mods\RebornFieldGuideLink.rb
echo Start (or restart) the game. A folder "RFGLink" with state.json appears in
echo %GAME% once you are on the map.
echo To uninstall, delete that file.
echo.
pause
exit /b 0

:fail
echo.
echo The mod was not installed.
pause
exit /b 1
