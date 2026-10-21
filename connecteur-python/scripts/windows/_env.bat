@echo off
REM ============================================================
REM T-CONNECTOR SFEC - Preparation de l'environnement Python
REM
REM A appeler depuis un autre script :
REM     call "%~dp0_env.bat"
REM
REM Ne pas lancer directement.
REM
REM Retourne :
REM   0 = OK
REM   1 = Python absent ou version/architecture incompatible
REM   2 = venv invalide
REM   3 = pip invalide
REM
REM Definit pour le script appelant :
REM   ROOT     = racine du projet (connecteur-python)
REM   VENV_PY  = chemin absolu vers .venv\Scripts\python.exe
REM   PY_CMD   = commande Python de base utilisee (ex: "py -3")
REM ============================================================

REM Pas de setlocal ici : ROOT, VENV_PY et PY_CMD doivent
REM remonter dans le script appelant.

for %%I in ("%~dp0..\..") do set "ROOT=%%~fI"

if not exist "%ROOT%\main.py" (
    echo [ERREUR] Racine du projet introuvable : "%ROOT%"
    echo Ce script doit rester dans connecteur-python\scripts\windows\.
    exit /b 1
)

REM Variables d'environnement qui peuvent casser un venv.
set "PYTHONHOME="
set "PYTHONPATH="
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

REM ------------------------------------------------------------
REM 1. Trouver un interpreteur Python
REM ------------------------------------------------------------
set "PY_CMD="

where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py -3"
)

if not defined PY_CMD (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PY_CMD=python"
    )
)

if not defined PY_CMD (
    echo [ERREUR] Python est introuvable.
    echo.
    echo Installez Python 3.12 64 bits depuis :
    echo   https://www.python.org/downloads/windows/
    echo Cochez "Add python.exe to PATH" pendant l'installation.
    echo Si Windows ouvre le Microsoft Store, desactivez les alias
    echo d'execution de python.exe dans les parametres Windows.
    exit /b 1
)

REM ------------------------------------------------------------
REM 2. Verifier la version et l'architecture de Python
REM ------------------------------------------------------------
%PY_CMD% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python 3.9 ou superieur est requis.
    echo Version detectee :
    %PY_CMD% --version
    echo.
    echo Installez Python 3.12 64 bits depuis :
    echo   https://www.python.org/downloads/windows/
    exit /b 1
)

%PY_CMD% -c "import struct, sys; sys.exit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python 32 bits detecte.
    echo Le connecteur et le driver ODBC SQL Server doivent etre en 64 bits.
    echo Installez la version "Windows installer (64-bit)" de Python.
    exit /b 1
)

%PY_CMD% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 echo [ATTENTION] Python 3.9/3.10 detecte - Python 3.11 ou 3.12 est recommande.

REM ------------------------------------------------------------
REM 3. Creer ou valider l'environnement virtuel
REM ------------------------------------------------------------
if not exist "%ROOT%\.venv\Scripts\python.exe" (
    echo [INFO] Creation de l'environnement virtuel .venv...
    %PY_CMD% -m venv "%ROOT%\.venv"
    if errorlevel 1 (
        echo [ERREUR] Creation du venv impossible.
        echo Verifiez que le module venv est disponible et que le dossier
        echo du projet est accessible en ecriture.
        exit /b 2
    )
)

set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [ERREUR] Le venv est incomplet : "%VENV_PY%" est absent.
    echo Supprimez le dossier .venv puis relancez ce script.
    exit /b 2
)

"%VENV_PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Le venv existe mais son interpreteur ne demarre pas.
    echo Le Python utilise pour le creer a probablement ete desinstalle
    echo ou mis a jour. Supprimez le dossier .venv puis relancez.
    exit /b 2
)

"%VENV_PY%" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Le venv utilise une version de Python trop ancienne.
    echo Supprimez le dossier .venv puis relancez ce script.
    exit /b 2
)

"%VENV_PY%" -c "import struct, sys; sys.exit(0 if struct.calcsize('P') == 8 else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Le venv est en 32 bits.
    echo Supprimez le dossier .venv puis relancez avec Python 64 bits.
    exit /b 2
)

call "%ROOT%\.venv\Scripts\activate.bat"

"%VENV_PY%" -c "import os, sys; v=os.environ.get('VIRTUAL_ENV',''); sys.exit(0 if sys.prefix != sys.base_prefix and v and os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(os.path.abspath(v)) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] L'environnement virtuel n'a pas pu etre active.
    echo Verifiez les droits sur le dossier .venv.
    exit /b 2
)

REM ------------------------------------------------------------
REM 4. Verifier et mettre a jour pip DANS le venv
REM ------------------------------------------------------------
"%VENV_PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] pip est absent du venv, reparation via ensurepip...
    "%VENV_PY%" -m ensurepip --upgrade
    if errorlevel 1 (
        echo [ERREUR] pip est introuvable dans le venv.
        echo Supprimez le dossier .venv puis relancez ce script.
        exit /b 3
    )
)

REM Verifie que pip appartient bien au venv et pas au Python systeme.
"%VENV_PY%" -c "import os, pip, sys; p=os.path.normcase(os.path.abspath(pip.__file__)); v=os.path.normcase(os.path.abspath(sys.prefix)); sys.exit(0 if p.startswith(v + os.sep) and sys.prefix != sys.base_prefix else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] pip ne pointe pas vers l'environnement virtuel.
    echo pip utilise :
    "%VENV_PY%" -m pip --version
    echo Attendu : %ROOT%\.venv
    echo Supprimez le dossier .venv puis relancez ce script.
    exit /b 3
)

REM pip >= 24.0 pour lire correctement les metadonnees et wheels modernes.
"%VENV_PY%" -c "import pip, sys; v=tuple(int(x) for x in pip.__version__.split('.')[:2]); sys.exit(0 if v >= (24, 0) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Mise a jour de pip...
    "%VENV_PY%" -m pip install --upgrade pip setuptools wheel --no-input --disable-pip-version-check --prefer-binary
    if errorlevel 1 (
        echo [ATTENTION] Mise a jour de pip impossible.
        echo Causes possibles : pas de reseau, proxy, droits insuffisants.
        echo On continue avec la version de pip deja installee.
    )
)

REM ------------------------------------------------------------
REM 5. Verifier la presence d'un driver ODBC SQL Server
REM ------------------------------------------------------------
set "ODBC_DRIVER_OK="
reg query "HKLM\SOFTWARE\ODBC\ODBCINST.INI\ODBC Drivers" 2>nul | findstr /I /C:"ODBC Driver 18 for SQL Server" >nul && set "ODBC_DRIVER_OK=1"
reg query "HKLM\SOFTWARE\ODBC\ODBCINST.INI\ODBC Drivers" 2>nul | findstr /I /C:"ODBC Driver 17 for SQL Server" >nul && set "ODBC_DRIVER_OK=1"
if not defined ODBC_DRIVER_OK (
    echo [ATTENTION] Aucun driver ODBC Microsoft SQL Server 17/18 detecte.
    echo Installez-le en 64 bits avant de connecter Sage :
    echo   https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server
)

REM ------------------------------------------------------------
REM 6. Diagnostic
REM ------------------------------------------------------------
echo.
echo [ENV] Python base :
%PY_CMD% --version
echo [ENV] Python venv :
"%VENV_PY%" --version
echo [ENV] Venv   : %ROOT%\.venv
"%VENV_PY%" -m pip --version
echo.

exit /b 0
