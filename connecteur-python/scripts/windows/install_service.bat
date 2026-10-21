@echo off
setlocal EnableExtensions
title T-CONNECTOR SFEC - Installation Service

REM ============================================================
REM T-CONNECTOR SFEC - Installation du service Windows
REM Necessite Python 3.9+ 64 bits et les droits administrateur.
REM ============================================================

echo ============================================
echo   T-CONNECTOR SFEC - Installation Service
echo ============================================
echo.

REM Verifier les droits administrateur.
net session >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Lancez ce script en tant qu'administrateur.
    echo Clic droit ^> Executer en tant qu'administrateur
    pause
    exit /b 1
)

REM Racine du projet = 2 niveaux au-dessus de scripts\windows\
cd /d "%~dp0..\.."
if errorlevel 1 (
    echo [ERREUR] Impossible d'acceder a la racine du projet.
    pause
    exit /b 1
)

REM Prepare Python, le venv et pip. Definit ROOT et VENV_PY.
call "%~dp0_env.bat"
if errorlevel 1 (
    echo.
    echo [ERREUR] Preparation de l'environnement Python impossible.
    pause
    exit /b 1
)

if not exist "%ROOT%\main.py" (
    echo [ERREUR] main.py introuvable dans "%ROOT%".
    pause
    exit /b 1
)

if not exist "%ROOT%\data" mkdir "%ROOT%\data"

REM Nettoyer un ancien fichier temporaire genere par les versions precedentes.
if exist "%ROOT%\_install_svc.py" del "%ROOT%\_install_svc.py" >nul 2>&1

echo [1/4] Installation des dependances...
"%VENV_PY%" -m pip install -r "%ROOT%\requirements-windows.txt" --prefer-binary --no-input --retries 3 --timeout 30 --disable-pip-version-check
if errorlevel 1 (
    echo [ERREUR] Echec de l'installation des dependances.
    echo Verifiez la connexion Internet ou le proxy, puis relancez.
    pause
    exit /b 1
)

echo.
echo [2/4] Recherche de NSSM...
where nssm >nul 2>&1
if errorlevel 1 goto :install_pyservice

REM ------------------------------------------------------------
REM Installation via NSSM (recommande : chemin Python explicite)
REM ------------------------------------------------------------
echo [3/4] Creation du service via NSSM...
set "SERVICE_NAME=TConnectorSFEC"

nssm status "%SERVICE_NAME%" >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Service existant detecte - arret et suppression...
    nssm stop "%SERVICE_NAME%" >nul 2>&1
    nssm remove "%SERVICE_NAME%" confirm >nul 2>&1
)

nssm install "%SERVICE_NAME%" "%VENV_PY%" "%ROOT%\main.py"
if errorlevel 1 (
    echo [ERREUR] Echec de la creation du service via NSSM.
    echo Le service existe peut-etre deja : lancez uninstall_service.bat
    echo puis relancez ce script.
    pause
    exit /b 1
)

nssm set "%SERVICE_NAME%" DisplayName "T-CONNECTOR SFEC - Connecteur Facturation Electronique"
nssm set "%SERVICE_NAME%" Description "Connecteur Python pour la certification SFEC"
nssm set "%SERVICE_NAME%" Start SERVICE_AUTO_START
nssm set "%SERVICE_NAME%" AppDirectory "%ROOT%"
nssm set "%SERVICE_NAME%" AppStdout "%ROOT%\data\output.log"
nssm set "%SERVICE_NAME%" AppStderr "%ROOT%\data\error.log"
nssm set "%SERVICE_NAME%" AppRotateFiles 1
nssm set "%SERVICE_NAME%" AppRotateBytes 10485760

echo [4/4] Demarrage du service...
nssm start "%SERVICE_NAME%"
if errorlevel 1 (
    echo [ATTENTION] Le service est installe mais n'a pas demarre.
    echo Consultez : %ROOT%\data\error.log
)
goto :done

REM ------------------------------------------------------------
REM Fallback : installation via pywin32
REM ------------------------------------------------------------
:install_pyservice
echo [INFO] NSSM introuvable - utilisation de pywin32.
echo [ATTENTION] Le mode pywin32 est moins fiable que NSSM avec un venv.
echo Si le service ne demarre pas, installez NSSM : https://nssm.cc/

"%VENV_PY%" -c "import win32service, win32serviceutil, servicemanager" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] pywin32 n'est pas disponible dans le venv.
    echo Relancez l'installation des dependances :
    echo   "%VENV_PY%" -m pip install -r "%ROOT%\requirements-windows.txt"
    echo Si l'import echoue avec "DLL load failed", executez en administrateur :
    echo   "%VENV_PY%" "%ROOT%\.venv\Scripts\pywin32_postinstall.py" -install
    pause
    exit /b 1
)

if not exist "%ROOT%\scripts\windows\_install_svc.py" (
    echo [ERREUR] scripts\windows\_install_svc.py introuvable.
    pause
    exit /b 1
)

echo [3/4] Creation du service via pywin32...
"%VENV_PY%" "%ROOT%\scripts\windows\_install_svc.py" --startup auto install
if errorlevel 1 (
    echo [ERREUR] Echec de la creation du service via pywin32.
    echo Le service existe peut-etre deja : lancez uninstall_service.bat
    echo puis relancez ce script.
    pause
    exit /b 1
)

echo [4/4] Demarrage du service...
sc start TConnectorSFEC
if errorlevel 1 (
    echo [ATTENTION] Le service est installe mais n'a pas demarre.
    echo Verifiez l'observateur d'evenements Windows et le dossier data\.
)
goto :done

:done
echo.
echo ============================================
echo   [OK] Service T-CONNECTOR SFEC installe
echo ============================================
echo.
echo Dashboard : http://localhost:3000
echo Logs      : %ROOT%\data\
echo Service   : TConnectorSFEC
echo.
pause
exit /b 0
