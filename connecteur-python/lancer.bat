@echo off
title T-CONNECTOR SFEC - Mode Portable
echo ============================================================
echo    T-CONNECTOR SFEC - Demarrage portable
echo ============================================================
echo.
echo    Dashboard : http://localhost:3000
echo    Ctrl+C pour arreter
echo.

:: Creer le dossier data
if not exist "data" mkdir "data"

:: Lancer l'exe
TConnector.exe

pause
