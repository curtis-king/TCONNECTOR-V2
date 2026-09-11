# Connexion du connecteur Python (WSL) à SQL Server sous Windows

Procédure complète pour permettre à `connecteur-python` (tournant dans WSL Ubuntu)
de se connecter à la base Sage 100 hébergée sur SQL Server (machine Windows hôte
ou serveur du réseau).

État des lieux relevé le 2026-09-11 sur cette machine :

| Composant | État |
|---|---|
| `pyodbc` (venv) | OK, importe sans erreur |
| Driver ODBC MS (`msodbcsql17/18`) | **absent** — `pyodbc.drivers()` renvoie `[]` |
| IP du host Windows vue depuis WSL | `172.31.16.1` |
| Port 1433 sur le host | **fermé / injoignable** depuis WSL |
| `config.json` (section `db`) | vide (server, database, user non renseignés) |

---

## Étape 1 — Vérifier que SQL Server tourne sous Windows

Dans **PowerShell Windows** (pas WSL) :

```powershell
# Lister les services SQL Server
Get-Service | Where-Object { $_.Name -like 'MSSQL*' -or $_.DisplayName -like '*SQL Server*' }
```

Un service `MSSQLSERVER` (instance par défaut) ou `MSSQL$<NOM_INSTANCE>`
(instance nommée) doit être à l'état **Running**.

Sinon, le démarrer :

```powershell
Start-Service MSSQLSERVER          # ou MSSQL$INSTANCE pour une instance nommée
```

Alternative graphique : `services.msc` → chercher "SQL Server (...)".

### 1.b — Le SQL Server de Sage est-il sur ce PC ?

Sage 100c utilise en général une instance SQL Server dédiée (souvent nommée,
ex. `MSSQL$SAGE` ou `.\SQLEXPRESS`). Pour identifier la bonne :

```powershell
Get-Service 'MSSQL$*' | Format-Table Name, Status, DisplayName
```

Note le nom exact de l'instance : il servira pour la chaîne `SERVER`.

---

## Étape 2 — Activer TCP/IP et fixer le port 1433

SQL Server n'écoute pas en TCP par défaut (surtout SQLEXPRESS). WSL ne peut le
joindre que via TCP.

1. Ouvrir **SQL Server Configuration Manager**
   (ou `C:\Windows\SysWOW64\SQLServerManager<version>.msc`).
2. `SQL Server Network Configuration` → `Protocols for <INSTANCE>`.
3. Activer **TCP/IP** (Enabled = Yes).
4. Double-clic sur TCP/IP → onglet **IP Addresses** → tout en bas, section
   **IPAll** :
   - `TCP Dynamic Ports` = (vider)
   - `TCP Port` = `1433`
5. Redémarrer le service :

```powershell
Restart-Service MSSQLSERVER -Force   # ou MSSQL$INSTANCE -Force
```

6. Vérifier que SQL Server écoute bien :

```powershell
Test-NetConnection -ComputerName localhost -Port 1433
# TcpTestSucceeded doit être True
```

> Note instance nommée : si tu laisses les ports dynamiques, il faut aussi le
> service **SQL Server Browser** (UDP 1434). Le plus simple reste de fixer 1433
> comme ci-dessus.

---

## Étape 3 — Ouvrir le firewall Windows

```powershell
New-NetFirewallRule -DisplayName "SQL Server 1433" `
  -Direction Inbound -Protocol TCP -LocalPort 1433 -Action Allow
```

---

## Étape 4 — Activer l'authentification SQL et créer un login

Depuis WSL/Linux, l'authentification Windows (`Trusted_Connection`) **ne
fonctionne pas**. Il faut un login SQL Server classique (UID/PWD).

1. Dans **SSMS** (ou Azure Data Studio) : clic droit sur le serveur →
   `Properties` → `Security` → cocher **SQL Server and Windows Authentication
   mode**. Redémarrer le service après changement.
2. Créer un login dédié au connecteur :

```sql
CREATE LOGIN tconnector WITH PASSWORD = '<MotDePasseFort>',
    CHECK_POLICY = OFF;
USE <BaseSage>;   -- ex. BIJOU, ou le nom de ta société Sage
CREATE USER tconnector FOR LOGIN tconnector;
ALTER ROLE db_datareader ADD MEMBER tconnector;
ALTER ROLE db_datawriter ADD MEMBER tconnector;  -- nécessaire pour write_sfec_to_invoice
```

> Le connecteur écrit dans F_DOCENTETE (colonnes SFEC_*) et exécute des
> `ALTER TABLE` (auto-migration SFEC). Pour la prod, `db_datawriter` suffit ;
> en première exécution, `db_ddladmin` peut être requis pour la création des
> colonnes SFEC si elles n'existent pas.

---

## Étape 5 — Installer le driver ODBC Microsoft dans WSL

Le driver `ODBC Driver 17/18 for SQL Server` doit exister **dans WSL**
(le driver Windows ne sert à rien côté Linux). État actuel : absent.

```bash
# Repo Microsoft (Ubuntu)
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | sudo gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg
curl -fsSL https://packages.microsoft.com/config/ubuntu/$(lsb_release -rs)/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list

sudo apt update
sudo ACCEPT_EULA=Y apt install -y msodbcsql18 unixodbc
# optionnel mais pratique pour tester : mssql-tools18 (fournit sqlcmd)
sudo ACCEPT_EULA=Y apt install -y mssql-tools18
```

Vérification :

```bash
odbcinst -q -d          # doit afficher [ODBC Driver 18 for SQL Server]
python3 -c "import pyodbc; print(pyodbc.drivers())"   # depuis le venv
```

> Le code (`database.py:199-205`) cherche dans l'ordre : Driver 17, 13, 11,
> "SQL Server", Native Client 11. Si seul le 18 est installé, il faut
> l'ajouter à la liste `preferred` dans `_build_conn_string`, ou installer
> `msodbcsql17`. **Point d'attention.**

---

## Étape 6 — Tester la joignabilité depuis WSL

IP du host Windows vue depuis WSL :

```bash
ip route show default | awk '{print $3}'    # actuellement 172.31.16.1
```

Test du port :

```bash
timeout 3 bash -c 'cat < /dev/null > /dev/tcp/172.31.16.1/1433' \
  && echo "1433 OUVERT" || echo "1433 FERME"
```

État au 2026-09-11 : **FERME** → refaire ce test après les étapes 1-3.

Test complet avec sqlcmd (si mssql-tools18 installé) :

```bash
/opt/mssql-tools18/bin/sqlcmd -S 172.31.16.1,1433 -U tconnector -P '<MotDePasse>' \
  -d <BaseSage> -C -Q "SELECT TOP 3 DO_Piece FROM F_DOCENTETE"
```

(`-C` = TrustServerCertificate, utile si certificat auto-signé.)

---

## Étape 7 — Remplir `config.json`

Fichier : `connecteur-python/config.json`, section `db` :

```json
"db": {
    "server": "172.31.16.1",
    "port": 1433,
    "database": "<BaseSage>",
    "user": "tconnector",
    "password": "<MotDePasseFort>",
    "trusted_connection": false,
    "encrypt": false,
    "trust_server_certificate": true
}
```

Notes :
- `server` = IP du host Windows (pas `localhost` — en mode NAT WSL, localhost
  pointe sur WSL lui-même).
- Instance nommée possible via `server: "172.31.16.1\\INSTANCE"` (dans ce cas
  le code n'ajoute pas de PORT, voir `database.py:213`), mais IP + port fixe
  est plus fiable.
- `trusted_connection` doit rester `false` depuis Linux.
- `config.json` contient le mot de passe en clair : ne pas le committer.

---

## Étape 8 — Lancer et vérifier

```bash
cd ~/projects/TCONNECTOR-V2/connecteur-python
.venv/bin/python3 main.py
```

Au démarrage, les logs doivent afficher :

```
Connexion SQL Server etablie: 172.31.16.1/<BaseSage>
Tables Sage 100: {'documents': 'F_DOCENTETE', ...}
```

Si `Configuration base de donnees incomplete` → `config.json` non lu
(vérifier qu'il est bien à la racine de `connecteur-python/`).

---

## Dépannage rapide

| Symptôme | Cause probable | Action |
|---|---|---|
| `libodbc.so.2: cannot open shared object file` | unixODBC manquant | `sudo apt install libodbc2 unixodbc` |
| `pyodbc.drivers()` renvoie `[]` | driver MS absent | étape 5 |
| `Driver ... not found` | seul le 18 installé, code cherche 17 | installer `msodbcsql17` ou ajouter "ODBC Driver 18 for SQL Server" dans `preferred` (`database.py:199`) |
| `1433 FERME` depuis WSL | TCP/IP désactivé / firewall / service arrêté | étapes 1-3 |
| `Login failed for user` | auth SQL désactivée ou mauvais login | étape 4 |
| Timeout à la connexion | instance nommée sans Browser, mauvais port | fixer 1433 (étape 2) |
