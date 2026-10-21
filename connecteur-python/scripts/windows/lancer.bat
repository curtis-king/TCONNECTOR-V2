@echo off
setlocal EnableExtensions
title T-CONNECTOR SFEC - Mode Portable

REM ============================================================
REM T-CONNECTOR SFEC - Demarrage portable
REM Ce script lance le binaire PyInstaller (TConnector.exe).
REM En developpement, utiliser start.bat.
REM ============================================================

echo ============================================================
echo    T-CONNECTOR SFEC - Demarrage portable
echo ============================================================
echo.
echo    Dashboard : http://localhost:3000
echo    Ctrl+C pour arreter
echo.

REM Racine du projet = 2 niveaux au-dessus de scripts\windows\
cd /d "%~dp0..\.."
if errorlevel 1 (
    echo [ERREUR] Impossible d'acceder a la racine du projet.
    pause
    exit /b 1
)

if not exist "TConnector.exe" (
    echo [ERREUR] TConnector.exe introuvable dans "%CD%".
    echo.
    echo Ce script lance la version portable ou installee.
    echo En developpement, utilisez start.bat.
    echo Pour construire le binaire, lancez PyInstaller puis installer.bat.
    pause
    exit /b 1
)

if not exist "data" mkdir "data"

TConnector.exe
set "EXITCODE=%errorlevel%"

echo.
if not "%EXITCODE%"=="0" (
    echo [ERREUR] TConnector.exe s'est arrete avec le code %EXITCODE%.
)

pause
exit /b %EXITCODE%
