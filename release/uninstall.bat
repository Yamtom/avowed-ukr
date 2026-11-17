@echo off
rem Avowed - removes the Ukrainian localization patch (and, with /fonts, the font patch).
rem Usage:  uninstall.bat ["<path to ...\Alabama\Content\Paks>"] [/fonts]
setlocal EnableExtensions
set "PAKS=M:\XboxGames\Avowed\Content\Alabama\Content\Paks"
set "FONTS=0"
:args
if "%~1"=="" goto run
if /I "%~1"=="/fonts" (set "FONTS=1") else (set "PAKS=%~1")
shift
goto args

:run
if not exist "%PAKS%\Alabama-WinGDK.pak" (
  echo ERROR: "%PAKS%\Alabama-WinGDK.pak" not found - this is not the Avowed Paks folder.
  exit /b 1
)
tasklist /FI "STATUS eq RUNNING" 2>nul | find /I "Avowed" >nul
if not errorlevel 1 (
  echo ERROR: Avowed is running. Close the game and run uninstall.bat again.
  exit /b 1
)
for %%E in (pak utoc ucas) do if exist "%PAKS%\Alabama-WinGDK_Ukr_P.%%E" del /Q "%PAKS%\Alabama-WinGDK_Ukr_P.%%E"
if "%FONTS%"=="1" (
  for %%E in (pak utoc ucas) do if exist "%PAKS%\Alabama-WinGDK_UkrFonts_P.%%E" del /Q "%PAKS%\Alabama-WinGDK_UkrFonts_P.%%E"
)
echo Removed. The original game files were never modified (Alabama-WinGDK.pak/.utoc/.ucas are untouched).
exit /b 0
