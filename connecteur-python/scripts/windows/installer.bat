@echo off
setlocal EnableExtensions
title Installation T-CONNECTOR SFEC
color 0A

REM ============================================================
REM T-CONNECTOR SFEC - Installation du bundle PyInstaller
REM Source : dist\TConnector\ (genere par PyInstaller)
REM ============================================================

echo ============================================================
echo    T-CONNECTOR SFEC - Installation
echo    Systeme de Facturation Electronique Certifie
echo ============================================================
echo.

REM Racine du projet = 2 niveaux au-dessus de scripts\windows\
for %%I in ("%~dp0..\..") do set "ROOT=%%~fI"
set "SOURCE=%ROOT%\dist\TConnector"
set "INSTALL_DIR=C:\TConnector"

REM Verifier admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERREUR] Lancez ce script en tant qu'administrateur !
    echo Clic droit ^> Executer en tant qu'administrateur
    pause
    exit /b 1
)

REM Verifier que le build existe
if not exist "%SOURCE%\TConnector.exe" (
    echo [ERREUR] Build introuvable : "%SOURCE%\TConnector.exe"
    echo.
    echo Generez d'abord le binaire avec PyInstaller, puis relancez ce script.
    echo En developpement, utilisez start.bat.
    pause
    exit /b 1
)

echo [INFO] Installation dans : %INSTALL_DIR%
echo.

REM Creer le dossier
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

REM Copier les fichiers du build
echo [1/4] Copie des fichiers...
xcopy /E /I /Y "%SOURCE%\*" "%INSTALL_DIR%\" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERREUR] Echec de la copie des fichiers.
    echo Source : %SOURCE%
    echo Destination : %INSTALL_DIR%
    pause
    exit /b 1
)
echo       Fichiers copies avec succes.

REM Creer le dossier data
echo [2/4] Creation du dossier de donnees...
if not exist "%INSTALL_DIR%\data" mkdir "%INSTALL_DIR%\data"

REM Installer le service Windows
echo [3/4] Installation du service Windows...
sc create TConnectorSFEC binPath= "\"%INSTALL_DIR%\TConnector.exe\"" start= auto DisplayName= "T-CONNECTOR SFEC - Connecteur Facturation Electronique" >nul 2>&1
if %errorlevel% equ 0 (
    echo       Service cree avec succes.
    sc description TConnectorSFEC "Connecteur Python pour la certification SFEC - Synchronise Sage 100 et certifie les factures." >nul 2>&1
) else (
    echo       [ATTENTION] Le service existe deja ou erreur de creation.
)

REM Demarrer le service
echo [4/4] Demarrage du service...
net start TConnectorSFEC >nul 2>&1
if %errorlevel% equ 0 (
    echo       Service demarre.
) else (
    echo       [INFO] Le service demarrera automatiquement au prochain demarrage.
)

REM Creer le raccourci bureau
echo Creation du raccourci Bureau...
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut([System.IO.Path]::Combine([Environment]::GetFolderPath('Desktop'), 'T-CONNECTOR SFEC.url')); $sc.TargetPath = 'http://localhost:3000'; $sc.Save()" >nul 2>&1

echo.
echo ============================================================
echo    Installation terminee !
echo ============================================================
echo.
echo    Dashboard : http://localhost:3000
echo    Login     : admin@topinfo.com
echo    Mot de passe : 2410@2026@@cf
echo.
echo    Service   : TConnectorSFEC
echo    Dossier   : %INSTALL_DIR%
echo.
echo ============================================================
echo.
pause
exit /b 0
