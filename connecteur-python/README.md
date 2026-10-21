# T-CONNECTOR SFEC - Connecteur Python

Connecteur entre Sage 100 (SQL Server) et l'API SFEC, avec dashboard web local,
generation de PDF conformes et service Windows.

Ce dossier contient l'application Python. Les scripts de lancement Windows sont
dans `scripts/windows/`.

## Prerequis

| Element | Version minimale | Recommande |
|---|---|---|
| Python (runtime) | **3.9** | **3.12 64 bits** (ou 3.11) |
| Python (tests) | **3.10** (`pytest>=9`) | 3.12 64 bits |
| Driver ODBC SQL Server | ODBC Driver 17 | ODBC Driver 18 |
| NSSM (service Windows) | optionnel | installe dans le `PATH` |
| SumatraPDF | optionnel | telecharge automatiquement au demarrage |

Points importants :

- **Python 64 bits obligatoire** : `pyodbc` doit utiliser le driver ODBC 64 bits.
- Installez Python depuis <https://www.python.org/downloads/windows/> en cochant
  **"Add python.exe to PATH"**. Evitez le Python du Microsoft Store.
- Python 3.9 est le plancher impose par `qrcode>=8.0`, `watchdog` et `pywin32`.
  Python 3.9/3.10 ne recoivent plus de mises a jour de securite : preferez 3.11
  ou 3.12.
- `requirements-dev.txt` contient `pytest>=9`, qui exige Python 3.10+.

## Demarrage rapide sous Windows

### Script automatique

```bat
cd connecteur-python
scripts\windows\start.bat
```

Le script :

1. detecte Python (`py -3` puis `python`) ;
2. verifie la version (>= 3.9) et l'architecture (64 bits) ;
3. cree `.venv` si necessaire ;
4. verifie que pip est bien celui du venv et le met a jour si besoin ;
5. verifie et installe les dependances de `requirements-windows.txt`
   (versions minimales comprises) ;
6. lance `main.py`.

Dashboard par defaut : <http://localhost:3000> (voir `config.json`,
section `dashboard`).

### Equivalent manuel

```bat
cd connecteur-python
py -3.12 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements-windows.txt
python main.py
```

Ne jamais utiliser `pip` seul : utilisez `python -m pip` pour etre certain
d'installer dans le venv.

## Service Windows

Lancez une invite de commandes **en administrateur** :

```bat
cd connecteur-python
scripts\windows\install_service.bat
```

Le script installe les dependances dans `.venv`, puis :

- utilise **NSSM** s'il est disponible dans le `PATH` (recommande : chemin
  explicite vers `.venv\Scripts\python.exe`) ;
- sinon bascule sur **pywin32** via `scripts\windows\_install_svc.py`.

Pour supprimer le service :

```bat
scripts\windows\uninstall_service.bat
```

NSSM est optionnel mais recommande : <https://nssm.cc/>. Sans NSSM, le service
est installe par pywin32 et demarre avec `pythonservice.exe`. Ce mode peut
necessiter des DLL supplementaires dans le venv selon la version de Python ;
si le service ne demarre pas, installez NSSM et relancez le script.

## Version portable / installee

`lancer.bat` et `installer.bat` lancent le binaire **PyInstaller**
(`TConnector.exe`). Ils n'utilisent pas de venv : Python et les dependances sont
deja embarques dans le binaire.

```bat
REM Apres un build PyInstaller dans dist\TConnector\
scripts\windows\installer.bat
REM Puis, pour relancer :
scripts\windows\lancer.bat
```

`installer.bat` copie `dist\TConnector\` vers `C:\TConnector` et cree le
service `TConnectorSFEC`.

## Tests

```bat
cd connecteur-python
.venv\Scripts\activate
python -m pip install -r requirements-dev.txt
python -m pytest
```

Sous Linux :

```bash
cd connecteur-python
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-linux.txt -r requirements-dev.txt
python -m pytest
```

## Depannage

### "Python est introuvable" ou Windows ouvre le Microsoft Store

Le `python.exe` du Microsoft Store est un alias. Installez Python depuis
python.org, puis desactivez les alias d'execution dans
**Parametres > Applications > Alias d'execution des applications**.

### Version de Python refusee

Le script exige Python 3.9+ 64 bits. Verifiez :

```bat
py -0p
python --version
python -c "import struct; print(struct.calcsize('P') * 8)"
```

La derniere commande doit afficher `64`.

### Le venv est casse

Symptomes : `No Python at ...`, `ModuleNotFoundError`, ou pip pointe vers le
Python systeme. Supprimez le dossier `.venv` puis relancez `start.bat` :

```bat
rmdir /S /Q .venv
scripts\windows\start.bat
```

Le script ne supprime jamais `.venv` automatiquement.

### pip / proxy / pas de reseau

Le script utilise toujours `python -m pip` et tente de mettre pip a jour. Si la
mise a jour echoue (proxy, hors ligne), il continue avec le pip existant. Pour
forcer une installation derriere un proxy :

```bat
set HTTP_PROXY=http://proxy:8080
set HTTPS_PROXY=http://proxy:8080
.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
```

Si pip est completement casse, il est reinitialise automatiquement via
`ensurepip`.

### `ModuleNotFoundError: No module named 'app'`

Lancez les scripts depuis `connecteur-python` (ou utilisez `start.bat`), pas
depuis `scripts\windows`.

### Erreur ODBC / connexion Sage impossible

Installez le **Microsoft ODBC Driver 17 ou 18 for SQL Server en 64 bits** :

<https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server>

Verifiez que l'architecture du driver correspond a celle de Python.

### `ImportError: DLL load failed` avec pywin32

Executez, en administrateur :

```bat
.venv\Scripts\python.exe .venv\Scripts\pywin32_postinstall.py -install
```

### Les QR codes n'apparaissent pas sur les PDF

`qrcode` doit etre installe avec Pillow. C'est le cas via
`qrcode[pil]>=8.0` dans `requirements.txt`. Verifiez :

```bat
.venv\Scripts\python.exe -c "import PIL, qrcode; print(PIL.__version__, qrcode.__version__)"
```

## Scripts Windows

| Script | Usage |
|---|---|
| `start.bat` | Developpement : venv + installation des deps + `main.py` |
| `install_service.bat` | Service Windows via NSSM ou pywin32 (admin) |
| `uninstall_service.bat` | Suppression du service |
| `lancer.bat` | Lance le binaire `TConnector.exe` (mode portable) |
| `installer.bat` | Installe le bundle `dist\TConnector` (admin) |
| `desinstaller.bat` | Desinstalle `C:\TConnector` et le service (admin) |
| `install-watch-folder.bat` | Cree les dossiers de surveillance PDF |
| `_env.bat` | Helper interne : Python / venv / pip (ne pas lancer seul) |
| `_install_svc.py` | Helper interne d'installation du service pywin32 |
