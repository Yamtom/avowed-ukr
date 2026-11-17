@echo off
rem Avowed - Ukrainian localization patch installer (IoStore patch Alabama-WinGDK_Ukr_P).
rem Usage:  install.bat ["<path to ...\Alabama\Content\Paks>"] [/fonts]
rem Default path: M:\XboxGames\Avowed\Content\Alabama\Content\Paks
setlocal EnableExtensions
set "PAKS=M:\XboxGames\Avowed\Content\Alabama\Content\Paks"
set "FONTS=0"
:args
if "%~1"=="" goto run
if /I "%~1"=="/fonts" (set "FONTS=1") else (set "PAKS=%~1")
shift
goto args

:run
set "HERE=%~dp0"
if not exist "%PAKS%\Alabama-WinGDK.pak" (
  echo ERROR: "%PAKS%\Alabama-WinGDK.pak" not found - this is not the Avowed Paks folder.
  echo Pass the right folder as the first argument.
  exit /b 1
)
for %%E in (pak utoc ucas) do if not exist "%HERE%Alabama-WinGDK_Ukr_P.%%E" (
  echo ERROR: %HERE%Alabama-WinGDK_Ukr_P.%%E is missing next to install.bat.
  exit /b 1
)
tasklist /FI "STATUS eq RUNNING" 2>nul | find /I "Avowed" >nul
if not errorlevel 1 (
  echo ERROR: Avowed is running. Close the game and run install.bat again.
  exit /b 1
)
echo Installing to "%PAKS%" ...
for %%E in (pak utoc ucas) do copy /Y "%HERE%Alabama-WinGDK_Ukr_P.%%E" "%PAKS%\" >nul || (echo ERROR: copy of .%%E failed & exit /b 1)
if "%FONTS%"=="1" (
  for %%E in (pak utoc ucas) do if not exist "%HERE%fonts\Alabama-WinGDK_UkrFonts_P.%%E" (
    echo ERROR: fonts\Alabama-WinGDK_UkrFonts_P.%%E is missing.
    exit /b 1
  )
  for %%E in (pak utoc ucas) do copy /Y "%HERE%fonts\Alabama-WinGDK_UkrFonts_P.%%E" "%PAKS%\" >nul || (echo ERROR: font copy .%%E failed & exit /b 1)
  echo Font patch installed too.
)
echo Done. Start the game and choose the POLISH (Polski) language: the patch replaces the Polish text slot.
exit /b 0
