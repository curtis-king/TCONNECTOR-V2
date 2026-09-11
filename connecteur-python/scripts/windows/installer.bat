@echo off
title Installation T-CONNECTOR SFEC
color 0A
echo ============================================================
echo    T-CONNECTOR SFEC - Installation
echo    Systeme de Facturation Electronique Certifie
echo ============================================================
echo.

REM Racine du projet = 2 niveaux au-dessus de scripts\windows\
set "ROOT=%~dp0..\.."

:: Verifier admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERREUR] Lancez ce script en tant qu'administrateur !
    echo Clic droit ^> Executer en tant qu'administrateur
    pause
    exit /b 1
)

:: Definir le dossier d'installation
set "INSTALL_DIR=C:\TConnector"
echo [INFO] Installation dans : %INSTALL_DIR%
echo.

:: Creer le dossier
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

:: Copier les fichiers
echo [1/4] Copie des fichiers...
xcopy /E /I /Y "%ROOT%\TConnector\*" "%INSTALL_DIR%\" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERREUR] Echec de la copie des fichiers
    pause
    exit /b 1
)
echo       Fichiers copies avec succes.

:: Creer le dossier data
echo [2/4] Creation du dossier de donnees...
if not exist "%INSTALL_DIR%\data" mkdir "%INSTALL_DIR%\data"

:: Installer le service Windows
echo [3/4] Installation du service Windows...
sc create TConnectorSFEC binPath= "\"%INSTALL_DIR%\TConnector.exe\"" start= auto DisplayName= "T-CONNECTOR SFEC - Connecteur Facturation Electronique" >nul 2>&1
if %errorlevel% equ 0 (
    echo       Service cree avec succes.
    sc description TConnectorSFEC "Connecteur Python pour la certification SFEC - Synchronise Sage 100 et certifie les factures." >nul 2>&1
) else (
    echo       [ATTENTION] Le service existe deja ou erreur.
)

:: Demarrer le service
echo [4/4] Demarrage du service...
net start TConnectorSFEC >nul 2>&1
if %errorlevel% equ 0 (
    echo       Service demarre.
) else (
    echo       [INFO] Le service demarrera automatiquement au prochain demarrage.
)

:: Creer le raccourci bureau
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