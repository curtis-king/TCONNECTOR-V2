@echo off
REM ============================================================
REM T-CONNECTOR SFEC - Desinstallation du service
REM ============================================================

echo Suppression du service T-CONNECTOR SFEC...
echo.

cd /d "%~dp0"

REM Essayer NSSM d'abord
where nssm >nul 2>&1
if not errorlevel 1 (
    nssm stop TConnectorSFEC
    nssm remove TConnectorSFEC confirm
    echo Service NSSM supprime.
    goto :end
)

REM Essayer pywin32
sc stop TConnectorSFEC >nul 2>&1
sc delete TConnectorSFEC >nul 2>&1

REM Aussi supprimer l'ancien nom
sc stop T-Connector-SFEC >nul 2>&1
sc delete T-Connector-SFEC >nul 2>&1

echo Service supprime.

:end
echo.
echo Termine.
pause
