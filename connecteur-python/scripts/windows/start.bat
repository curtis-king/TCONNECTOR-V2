@echo off
setlocal EnableExtensions
title T-CONNECTOR SFEC - Mode Developpement

REM ============================================================
REM T-CONNECTOR SFEC - Lancement en mode developpement
REM (Ne pas utiliser en production - utiliser install_service.bat)
REM ============================================================

echo ============================================
echo   T-CONNECTOR SFEC - Mode Developpement
echo ============================================
echo.

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

echo [INFO] Verification des dependances...
"%VENV_PY%" -m pip install -r "%ROOT%\requirements-windows.txt" --prefer-binary --no-input --retries 3 --timeout 30 --disable-pip-version-check --quiet
if errorlevel 1 (
    echo [ERREUR] Installation des dependances impossible.
    echo Verifiez la connexion Internet ou le proxy, puis relancez.
    pause
    exit /b 1
)
echo [OK] Dependances a jour.

REM Controle de coherence des paquets (non bloquant).
"%VENV_PY%" -m pip check
if errorlevel 1 (
    echo [ATTENTION] pip check signale des conflits de dependances.
    echo Le connecteur va quand meme demarrer.
)

echo.
echo [INFO] Demarrage de T-CONNECTOR...
echo Dashboard : http://localhost:3000 - voir config.json
echo Appuyez sur Ctrl+C pour arreter.
echo.

"%VENV_PY%" main.py
set "EXITCODE=%errorlevel%"

echo.
if not "%EXITCODE%"=="0" (
    echo [ERREUR] T-CONNECTOR s'est arrete avec le code %EXITCODE%.
    echo Consultez les logs dans le dossier data\.
)

pause
exit /b %EXITCODE%
