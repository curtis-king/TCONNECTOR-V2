@echo off
title Desinstallation T-CONNECTOR SFEC
color 0C
echo ============================================================
echo    T-CONNECTOR SFEC - Desinstallation
echo ============================================================
echo.

:: Verifier admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERREUR] Lancez ce script en tant qu'administrateur !
    pause
    exit /b 1
)

:: Arreter le service
echo [1/4] Arret du service...
net stop TConnectorSFEC >nul 2>&1
echo       Service arrete.

:: Supprimer le service
echo [2/4] Suppression du service Windows...
sc delete TConnectorSFEC >nul 2>&1
echo       Service supprime.

:: Supprimer le raccourci bureau
echo [3/4] Suppression du raccourci Bureau...
del "%USERPROFILE%\Desktop\T-CONNECTOR SFEC.url" >nul 2>&1

:: Supprimer les fichiers
echo [4/4] Suppression des fichiers...
set "INSTALL_DIR=C:\TConnector"
if exist "%INSTALL_DIR%" (
    rmdir /S /Q "%INSTALL_DIR%" >nul 2>&1
    echo       Fichiers supprimes.
) else (
    echo       Aucun dossier trouve.
)

:: Supprimer le raccourci menu demarrer
if exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\T-CONNECTOR SFEC" (
    rmdir /S /Q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\T-CONNECTOR SFEC" >nul 2>&1
)

echo.
echo ============================================================
echo    Desinstallation terminee !
echo ============================================================
echo.
pause
