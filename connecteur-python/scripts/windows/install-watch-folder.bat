@echo off
title T-CONNECTOR SFEC - Dossiers de surveillance impression
echo ============================================================
echo   Installation des dossiers de surveillance PDF (print_hijack)
echo ============================================================
echo.

REM Racine du projet = 2 niveaux au-dessus de scripts\windows\
pushd "%~dp0..\.."
set "WATCH=data\WATCH"

if not exist "%WATCH%\IN"         mkdir "%WATCH%\IN"
if not exist "%WATCH%\OUT"        mkdir "%WATCH%\OUT"
if not exist "%WATCH%\DONE"       mkdir "%WATCH%\DONE"
if not exist "%WATCH%\QUARANTINE" mkdir "%WATCH%\QUARANTINE"

echo Dossiers crees :
echo   %CD%\%WATCH%\IN          ^<- deposez ici les PDF Sage (nom = n^o facture)
echo   %CD%\%WATCH%\OUT         ^<- PDF conformes generes
echo   %CD%\%WATCH%\DONE        ^<- originaux traites (archives)
echo   %CD%\%WATCH%\QUARANTINE  ^<- non identifies / trop volumineux
echo.

set "SUMATRA=C:\Program Files\SumatraPDF\SumatraPDF.exe"
if not exist "%SUMATRA%" set "SUMATRA=%LOCALAPPDATA%\SumatraPDF\SumatraPDF.exe"
if exist "%SUMATRA%" (
    echo SumatraPDF : OK ^(%SUMATRA%^)
) else (
    echo [ATTENTION] SumatraPDF introuvable.
    echo   Installez-le puis renseignez print_hijack.sumatra_path dans config.json :
    echo   winget install --id SumatraPDF.SumatraPDF
)
echo.
echo Pensez a activer print_hijack.enabled = true dans config.json
popd
pause
