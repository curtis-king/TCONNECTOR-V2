@echo off
REM ============================================================
REM T-CONNECTOR SFEC - Installation du service Windows
REM Necessite Python 3.9+ et les droits administrateur
REM ============================================================

echo ============================================
echo   T-CONNECTOR SFEC - Installation Service
echo ============================================
echo.

cd /d "%~dp0"

REM Verifier Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python n'est pas installe ou n'est pas dans le PATH
    echo Telechargez Python 3.9+ depuis https://www.python.org/downloads/
    echo cochez "Add Python to PATH" lors de l'installation
    pause
    exit /b 1
)

echo [1/5] Installation des dependances...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERREUR] Echec installation des dependances
    pause
    exit /b 1
)

echo.
echo [2/5] Installation du service via NSSM...

REM Verifier si NSSM est disponible
where nssm >nul 2>&1
if errorlevel 1 (
    echo NSSM non trouve dans le PATH.
    echo.
    echo Option alternative: installation via pywin32 (sc create)
    echo.
    goto :install_pyservice
)

REM Installer via NSSM
set SERVICE_NAME=TConnectorSFEC
set PYTHON_PATH=python
set SCRIPT_PATH=%~dp0main.py

echo Creation du service NSSM: %SERVICE_NAME%
nssm install %SERVICE_NAME% "%PYTHON_PATH%" "%SCRIPT_PATH%"
nssm set %SERVICE_NAME% DisplayName "T-CONNECTOR SFEC - Connecteur Facturation Electronique"
nssm set %SERVICE_NAME% Description "Connecteur Python pour la certification SFEC"
nssm set %SERVICE_NAME% Start SERVICE_AUTO_START
nssm set %SERVICE_NAME% AppDirectory "%~dp0"
nssm set %SERVICE_NAME% AppStdout "%~dp0data\output.log"
nssm set %SERVICE_NAME% AppStderr "%~dp0data\error.log"
nssm set %SERVICE_NAME% AppRotateFiles 1
nssm set %SERVICE_NAME% AppRotateBytes 10485760

echo Demarrage du service...
nssm start %SERVICE_NAME%

echo.
echo [OK] Service installe et demarre!
echo.
echo Verifiez: http://localhost:3000
echo Logs: %~dp0data\output.log
goto :end

:install_pyservice
echo.
echo Installation via pywin32...
echo.

REM Creer le script d'installation du service
echo import win32serviceutil > "%~dp0_install_svc.py"
echo import sys >> "%~dp0_install_svc.py"
echo sys.path.insert(0, "%~dp0.") >> "%~dp0_install_svc.py"
echo from service import TConnectorService >> "%~dp0_install_svc.py"
echo win32serviceutil.HandleCommandLine(TConnectorService) >> "%~dp0_install_svc.py"

echo Installation du service Windows...
python "%~dp0_install_svc.py" install
if errorlevel 1 (
    echo [ERREUR] Echec installation service
    pause
    exit /b 1
)

echo Configuration du demarrage automatique...
sc config TConnectorSFEC start= auto
sc description TConnectorSFEC "Connecteur Python pour la certification SFEC - Synchronise Sage 100 et certifie les factures electroniques"

echo Demarrage du service...
sc start TConnectorSFEC

del "%~dp0_install_svc.py" 2>nul

echo.
echo [OK] Service installe et demarre!
echo Verifiez: http://localhost:3000

:end
echo.
pause
