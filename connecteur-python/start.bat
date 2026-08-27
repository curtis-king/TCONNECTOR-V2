@echo off
REM ============================================================
REM T-CONNECTOR SFEC - Lancement en mode developpement
REM (Ne pas utiliser en production - utiliser install_service.bat)
REM ============================================================

echo ============================================
echo   T-CONNECTOR SFEC - Mode Developpement
echo ============================================
echo.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python non trouve
    pause
    exit /b 1
)

echo Installation des dependances...
pip install -r requirements.txt -q

echo.
echo Demarrage de T-CONNECTOR...
echo Dashboard: http://localhost:3000
echo Appuyez sur Ctrl+C pour arreter
echo.

python main.py

pause
