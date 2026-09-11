@echo off
title T-CONNECTOR SFEC - Mode Portable
echo ============================================================
echo    T-CONNECTOR SFEC - Demarrage portable
echo ============================================================
echo.
echo    Dashboard : http://localhost:3000
echo    Ctrl+C pour arreter
echo.

REM Racine du projet = 2 niveaux au-dessus de scripts\windows\
cd /d "%~dp0..\.."

:: Creer le dossier data
if not exist "data" mkdir "data"

:: Lancer l'exe
TConnector.exe

pause