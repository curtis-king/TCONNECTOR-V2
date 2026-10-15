@echo off
REM ============================================================
REM T-CONNECTOR SFEC - Lancement en mode developpement
REM (Ne pas utiliser en production - utiliser install_service.bat)
REM ============================================================

echo ============================================
echo   T-CONNECTOR SFEC - Mode Developpement
echo ============================================
echo.

cd /d "%~dp0..\.."

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python non trouve
    pause
    exit /b 1
)

if not exist ".venv\Scripts\activate.bat" (
    echo Environnement virtuel introuvable, creation de .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERREUR] Creation du venv impossible
        pause
        exit /b 1
    )
)

echo Activation de l'environnement virtuel...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERREUR] Activation du venv impossible
    pause
    exit /b 1
)

echo Verification des dependances...
python -c "import importlib.util,sys;req={'pyodbc':'pyodbc','flask':'flask','reportlab':'reportlab','waitress':'waitress','qrcode':'qrcode','win32service':'pywin32'};miss=[n for m,n in req.items() if importlib.util.find_spec(m) is None];print('MANQUANTS:'+','.join(miss) if miss else 'DEPS_OK');sys.exit(1 if miss else 0)" >nul 2>&1
if errorlevel 1 (
    echo Dependances manquantes, installation...
    python -m pip install -r requirements-windows.txt
    if errorlevel 1 (
        echo [ERREUR] Installation des dependances impossible
        pause
        exit /b 1
    )
) else (
    echo Dependances deja installees.
)

echo.
echo Demarrage de T-CONNECTOR...
echo Dashboard: http://localhost:3000
echo Appuyez sur Ctrl+C pour arreter
echo.

python main.py

pause
