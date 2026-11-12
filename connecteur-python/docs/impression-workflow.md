# Workflow d'impression automatique - T-CONNECTOR SFEC

Ce document decrit le fonctionnement reel du module `print_hijack` et les
etapes pour l'activer proprement en production.

## 1. Objectif

Le systeme garantit qu'une facture Sage sort sur papier au format **conforme
SFEC** : mentions legales, bloc SFEC, QR code, mention `Original` ou
`Duplicata`.

Il couvre trois circuits :

| Circuit | Declencheur | Impression |
|---|---|---|
| Facture A4 Sage | PDF depose dans `data/WATCH/IN` | Silencieuse via SumatraPDF |
| Ticket POS 80 mm | Validation d'une vente dans le POS | Silencieuse via SumatraPDF |
| Navigateur / PDF | Bouton Imprimer ou telechargement | Via le navigateur, non silencieuse |

> Important : le connecteur **n'intercepte pas** le bouton Imprimer de Sage.
> L'utilisateur doit enregistrer le PDF dans le dossier surveille. Le watcher
> fait ensuite le reste.

## 2. Schema general

```text
Sage
  |  Imprimer -> Enregistrer en PDF
  v
data/WATCH/IN
  |
  |  FolderWatcher (watchdog + scan periodique)
  v
Identification facture (nom du fichier, puis texte PDF)
  |
  v
Controle SFEC + anti-doublon
  |
  +--> PDF conforme genere dans data/WATCH/OUT
  |         |
  |         v
  |    SumatraPDF -print-to "<imprimante A4>" -silent
  |         |
  |         +--> OK : original deplace dans data/WATCH/DONE
  |         +--> KO : original conserve dans IN (retry)
  |
  +--> Non identifie / refuse / trop gros : data/WATCH/QUARANTINE
```

## 3. Composants

| Fichier | Role |
|---|---|
| `app/print_hijack/__init__.py` | Configuration, demarrage/arret, log `data/print_hijack.log` |
| `app/print_hijack/folder_watcher.py` | Surveillance du dossier, stabilite, workflow, statuts |
| `app/print_hijack/job_mapper.py` | Identification de la facture |
| `app/print_hijack/auto_printer.py` | SumatraPDF, hash SHA-256, registre SQLite |
| `app/print_hijack/sites.py` | Sites, imprimantes, kill-switch, dry-run |
| `app/core/bootstrap.py` | Verification et installation automatique de SumatraPDF |
| `app/domain/pdf.py` | Generation du PDF conforme (QR, mentions, filigrane) |
| `app/domain/print_copies.py` | Journal `Original` / `Duplicata` |
| `app/web/routes/dashboard.py` | Page `/print` (historique) |
| `app/web/routes/sites.py` | Page `/sites` (configuration multi-sites) |
| `scripts/windows/install-watch-folder.bat` | Creation des dossiers de surveillance |
| `scripts/test_print_e2e.py` | Test bout-en-bout dry-run ou reel |

## 4. Prerequis

- Windows 64 bits pour l'impression reelle.
- Python 3.11/3.12 64 bits, ou binaire `TConnector.exe`.
- SumatraPDF. Il est telecharge automatiquement au demarrage si
  `auto_install_sumatra=true` et si le reseau est disponible.
- Une imprimante A4 pour les factures.
- Une imprimante ticket 80 mm si le POS doit imprimer silencieusement.
- Les dependances Python : `watchdog`, `pypdf`, `reportlab`, `qrcode[pil]`.

## 5. Configuration

### 5.1 Section `print_hijack` de `config.json`

```json
"print_hijack": {
  "enabled": true,
  "watch_dir": "data/WATCH/IN",
  "out_dir": "data/WATCH/OUT",
  "done_dir": "data/WATCH/DONE",
  "quarantine_dir": "data/WATCH/QUARANTINE",
  "printer_name": "",
  "sumatra_path": "",
  "sumatra_min_version": "3.5.2",
  "auto_install_sumatra": true,
  "stable_delay_s": 2,
  "stable_retries": 5,
  "poll_interval_s": 5,
  "dedup_window_min": 10,
  "max_pages": 50,
  "max_size_mb": 20,
  "wait_for_certification": true,
  "certif_timeout_min": 15,
  "on_uncertified": "watermark",
  "dry_run": false
}
```

| Cle | Role |
|---|---|
| `enabled` | Active le watcher A4. Sans cela, aucun PDF n'est traite. |
| `watch_dir` | Dossier surveille. Relatif a la racine de l'app, ou absolu. |
| `out_dir` | PDF conformes generes. |
| `done_dir` | Originaux traites et archives. |
| `quarantine_dir` | PDF non identifies, refuses ou dangereux. |
| `printer_name` | Imprimante A4 de secours si aucun site n'est configure. |
| `sumatra_path` | Chemin explicite de SumatraPDF. Vide = detection automatique. |
| `sumatra_min_version` | Version minimale acceptee par le bootstrap. |
| `auto_install_sumatra` | Telechargement automatique si absent ou trop ancien. |
| `stable_delay_s` | Duree de stabilite du fichier avant traitement. |
| `stable_retries` | Nombre de tentatives de stabilite. |
| `poll_interval_s` | Intervalle du scan periodique de secours. |
| `dedup_window_min` | Fenetre anti-doublon par numero de facture. |
| `max_pages` | Nombre maximal de pages accepte. |
| `max_size_mb` | Taille maximale du PDF. |
| `wait_for_certification` | Attend la certification SFEC avant impression. |
| `certif_timeout_min` | Delai maximal d'attente de certification. |
| `on_uncertified` | `watermark` = imprime une copie filigranee ; `quarantine` = bloque. |
| `dry_run` | Simule l'impression. Le fichier est quand meme archive dans DONE. |

### 5.2 Sites et imprimantes (`data/sites.json`)

La configuration multi-sites est stockee dans `data/sites.json`, cree
automatiquement au premier acces a `/sites`.

```json
{
  "paused": false,
  "dry_run": false,
  "sites": [
    {
      "id": "boutique-01",
      "name": "Boutique Kinshasa",
      "users": ["hp", "caisse1"],
      "machine": "PC-BOUTIQUE-01",
      "a4_printer": "HP LaserJet M404",
      "ticket_printer": "EPSON TM-T20",
      "enabled": true
    }
  ]
}
```

Resolution du site actif :

1. correspondance avec l'utilisateur Windows (`users`) ;
2. correspondance avec le hostname (`machine`) ;
3. si un seul site actif existe, il est utilise par defaut ;
4. sinon, aucun site n'est resolu et le fichier part en quarantaine.

Regles importantes :

- seules les imprimantes declarees dans un site actif sont utilisees ;
- si le site n'a pas d'imprimante A4, le fichier part en quarantaine ;
- si l'imprimante A4 est absente, le fichier reste dans `IN` avec le statut
  `IMPRIMANTE_ABSENTE` et sera repris plus tard ;
- `paused=true` bloque toutes les impressions ;
- `dry_run=true` simule l'impression ;
- `printer_name` n'est utilise qu'en mode legacy, quand aucun site n'existe.

La page `/sites` permet de creer les sites, de choisir les imprimantes et
d'activer le kill-switch ou le mode test.

### 5.3 Dossiers

Le chemin par defaut est `<racine de l'application>/data/WATCH/IN`.

| Installation | Chemin des PDF a deposer |
|---|---|
| Setup Inno Setup | `C:\Program Files\TConnector\data\WATCH\IN` |
| `installer.bat` | `C:\TConnector\data\WATCH\IN` |
| Version portable | `<dossier de TConnector.exe>\data\WATCH\IN` |
| Developpement | `...\connecteur-python\data\WATCH\IN` |

On peut mettre un chemin absolu ou un partage reseau :

```json
"watch_dir": "C:/TConnector/WATCH/IN"
"watch_dir": "//SRV-FICHIERS/Partage/TConnector/WATCH/IN"
```

Points importants :

- `watch_dir` doit etre un chemin du systeme de fichiers. Une URL FTP ou SFTP
  n'est pas supportee nativement.
- un partage SMB/UNC fonctionne, mais le compte du service doit y avoir acces ;
- evitez un lecteur mappe (`Z:`) pour un service Windows : utilisez un chemin
  UNC ;
- si le dossier est dans `C:\Program Files`, les utilisateurs standard n'ont
  generalement pas le droit d'y ecrire. Utilisez plutot un dossier dedie avec
  des droits Modify, par exemple `C:\TConnector\WATCH\IN`.

Le script `scripts\windows\install-watch-folder.bat` cree les quatre dossiers
et affiche les chemins.

## 6. Activation

1. Renseigner l'imprimante A4 :
   - soit `print_hijack.printer_name` dans `config.json` ;
   - soit un site dans `/sites` avec `a4_printer` (recommande).
2. Verifier que `print_hijack.enabled` vaut `true`.
3. Creer les dossiers :
   ```bat
   scripts\windows\install-watch-folder.bat
   ```
   Le watcher les cree aussi automatiquement au demarrage.
4. Verifier SumatraPDF :
   ```bat
   dir data\bin\SumatraPDF\SumatraPDF.exe
   ```
   S'il est absent, laisser `auto_install_sumatra=true` et demarrer l'app avec
   un acces reseau, ou installer SumatraPDF manuellement.
5. Redemarrer le service ou l'application :
   ```bat
   net stop TConnectorSFEC
   net start TConnectorSFEC
   ```
   En developpement : `scripts\windows\start.bat`.
6. Verifier le log :
   ```bat
   type data\print_hijack.log
   ```
   et la page `/print`.

> Ne pas activer le watcher avec `printer_name` vide et aucun site configure.
> Le watcher demarre, mais les PDF sont retenus dans `IN` avec le statut
> `IMPRIMANTE_ABSENTE` (si la facture est par ailleurs valide/certifiee).
> C'est sans danger, mais rien ne s'imprime.

## 7. Workflow detaille d'une facture A4

1. L'utilisateur imprime la facture depuis Sage et l'enregistre en PDF dans
   `data/WATCH/IN`, avec le numero de facture dans le nom :
   `FA00010.pdf`, `FV-0001.pdf`, `scan_FA00005_final.pdf`.
2. Le watcher detecte le fichier via `watchdog`, et le scan periodique sert de
   secours.
3. Il attend que la taille du fichier soit stable pendant `stable_delay_s` et
   que le fichier soit ouvrable.
4. Il verifie le kill-switch `paused`.
5. Il resout le site actif et l'imprimante A4.
6. Il refuse les PDF de plus de `max_size_mb` ou `max_pages`.
7. Il calcule le SHA-256 et consulte `processed_pdf_prints` :
   - deja `IMPRIME` : deplacement dans `DONE` ;
   - `EN_COURS` : nouvelle tentative apres un crash ;
   - meme numero imprime recemment : `SKIP_DEDUP`, deplacement dans `DONE`.
8. Il identifie la facture par le nom du fichier, puis par le texte de la
   premiere page avec `pypdf`.
9. Il applique la politique de certification SFEC (voir ci-dessous).
10. Il genere le PDF conforme dans `OUT`, avec la mention `Original` ou
    `Duplicata`.
11. Il imprime via SumatraPDF :
    ```text
    SumatraPDF.exe -print-to "<imprimante>" -silent "<pdf>"
    ```
12. En cas de succes, il enregistre `IMPRIME`, ajoute une ligne dans
    `invoice_print_log` et deplace l'original dans `DONE`.
13. En cas d'echec, il enregistre `ERREUR_IMPRESSION` et laisse l'original
    dans `IN` pour une nouvelle tentative.

### Politique de certification

| Statut SFEC | Facture validee | Resultat |
|---|---|---|
| `CERTIFIE` ou `DEJA_CERTIFIE` | peu importe | `IMPRIME` |
| tout statut | non (`valide != 1`) | `REFUS` -> quarantaine |
| vide, `EN_COURS`, `A_SURVEILLER` | oui | `ATTENTE_CERTIF` si delai < `certif_timeout_min` |
| vide, `EN_COURS`, `A_SURVEILLER` | oui, delai depasse | `NON_CONFORME` avec filigrane, ou quarantaine |
| `ERREUR` ou autre | oui | `NON_CONFORME` avec filigrane, ou quarantaine |

Avec `on_uncertified="watermark"`, le systeme imprime une copie portant le
filigrane `COPIE NON CERTIFIEE`. Si le client interdit toute impression non
certifiee, utiliser `"on_uncertified": "quarantine"`.

### Statuts

| Statut | Signification | Suite |
|---|---|---|
| `IMPRIME` | Impression remise au spooler | Original dans `DONE` |
| `NON_CONFORME` | Copie filigranee imprimee | Original dans `DONE` |
| `ATTENTE_CERTIF` | Certification SFEC en cours | Fichier conserve dans `IN` |
| `REFUS` | Facture non validee | Fichier dans `QUARANTINE` |
| `QUARANTINE` | Facture non identifiee, trop grosse ou politique de refus | Fichier dans `QUARANTINE` |
| `EN_COURS` | Impression en cours | Reprise si crash |
| `ERREUR` | Generation du PDF echouee | Fichier dans `QUARANTINE` |
| `ERREUR_IMPRESSION` | SumatraPDF ou imprimante en echec | Fichier conserve dans `IN`, retry |
| `IMPRIMANTE_ABSENTE` | Aucune imprimante configuree ou imprimante absente | Fichier conserve dans `IN`, pas de PDF dans `OUT` |
| `SKIP_DEDUP` | Meme facture imprimee recemment | Fichier dans `DONE` |

## 8. Workflow ticket POS 80 mm

Apres validation d'une vente, le POS appelle :

```text
POST /api/pos/ticket/<ticket_id>/print-silent
```

Le endpoint :

1. verifie que le ticket existe ;
2. resout le site actif ;
3. exige un `ticket_printer` dans le site ;
4. verifie que l'imprimante est connectee et que le kill-switch est inactif ;
5. genere le PDF ticket ;
6. imprime via SumatraPDF ;
7. renvoie `success`, `printer` et `dry_run`.

Si aucun site ou aucune imprimante ticket n'est configure, le POS affiche un
avertissement et le ticket reste consultable dans l'historique. L'impression
silencieuse du ticket est independante de `print_hijack.enabled` ; elle depend
de `/sites`.

## 9. Suivi et exploitation

- **`/print`** : 200 dernieres impressions, statut, statut SFEC, numero de
  certificat, imprimante, fichier et erreur.
- **`/sites`** : sites, imprimantes, site actif, pause et dry-run.
- **`data/print_hijack.log`** : log rotatif 5 Mo x 3.
- **`processed_pdf_prints`** : registre par hash de fichier.
- **`invoice_print_log`** : journal des impressions `Original` / `Duplicata`,
  tous canaux confondus (navigateur, PDF, watcher).

## 10. Tests

Toujours arreter le service avant un test manuel : deux watchers sur le meme
dossier se concurrencent.

### Dry-run

```bat
cd connecteur-python
.venv\Scripts\python.exe scripts\test_print_e2e.py
```

Le script depose `test_invoice.pdf` dans `IN` sous le nom d'une facture
existante, puis verifie :

- generation du PDF dans `OUT` ;
- original deplace dans `DONE` ;
- statut `IMPRIME` dans `processed_pdf_prints`.

En dry-run, le fichier est quand meme archive dans `DONE` : utilisez une copie
de test, pas une facture reelle.

### Impression reelle

```bat
.venv\Scripts\python.exe scripts\test_print_e2e.py --real --numero FA00010 --printer "NOM IMPRIMANTE"
```

Verifier ensuite la sortie papier, `/print` et `data/print_hijack.log`.

### Sous Linux

L'impression reelle n'est pas disponible (pas de `win32print` ni SumatraPDF).
Le dry-run fonctionne si les dependances sont installees :

```bash
cd connecteur-python
source .venv/bin/activate
python -m pip install -r requirements-linux.txt
python scripts/test_print_e2e.py
```

## 11. Depannage

| Symptome | Cause probable | Action |
|---|---|---|
| Le watcher ne demarre pas | `enabled=false` ou config non rechargee | Passer a `true`, redemarrer le service |
| Les PDF restent dans `IN` | `IMPRIMANTE_ABSENTE`, `ERREUR_IMPRESSION`, fichier verrouille | Configurer l'imprimante, verifier SumatraPDF et le log |
| Statut `ATTENTE_CERTIF` | Facture pas encore certifiee SFEC | Attendre, verifier le statut dans Sage/SFEC |
| Statut `QUARANTINE` | Nom sans numero, facture absente, PDF trop gros, facture non validee | Renommer avec le numero, valider la facture, verifier les limites |
| `IMPRIME` mais aucune feuille | Travail remis au spooler mais imprimante hors ligne/bourrage | Verifier l'imprimante et la file Windows |
| Pas de QR code | `qrcode` ou Pillow absent | `python -m pip install -r requirements.txt` |
| `SumatraPDF introuvable` | Bootstrap hors ligne ou chemin invalide | Installer SumatraPDF ou renseigner `sumatra_path` |
| Acces refuse sur un partage | Compte du service sans droits | Donner Modify au compte du service, utiliser UNC |
| `ftp://` dans `watch_dir` | FTP non supporte | Utiliser SMB/UNC ou un agent de synchronisation |

## 12. Limites et securite

- **Pas d'interception de Sage** : le PDF doit etre depose manuellement dans
  `IN`. Le systeme ne clique pas a la place de l'utilisateur.
- **Windows uniquement pour l'impression reelle** : SumatraPDF et
  `win32print` ne fonctionnent pas sous Linux.
- **Succes spooler != papier sorti** : SumatraPDF rend la main apres remise au
  spooler. Un bourrage ou une imprimante hors ligne peut ne pas etre detecte.
- **Pas de backoff** : les echecs `ERREUR_IMPRESSION` sont retentes a chaque
  scan. Surveillez `OUT`, `DONE` et `QUARANTINE` ; aucune purge automatique
  n'est implementee.
- **Multi-sites local** : `sites.json` est local a la machine. Il n'y a pas de
  serveur central ni de file `print_jobs`.
- **Pas de FTP** : `watch_dir` est un chemin local ou un partage monte.
- **Dry-run** : l'historique affiche `IMPRIME` meme en simulation, et le
  fichier est deplace dans `DONE`.
- **Politique non conforme** : avec `watermark`, une facture non certifiee peut
  sortir apres le timeout. Valider ce choix avec le client.

## 13. Checklist de mise en production

- [ ] `print_hijack.enabled = true`.
- [ ] `printer_name` renseigne, ou site actif avec `a4_printer`.
- [ ] Site POS avec `ticket_printer` si le POS imprime.
- [ ] SumatraPDF present dans `data/bin/SumatraPDF` ou chemin configure.
- [ ] Dossiers `IN`, `OUT`, `DONE`, `QUARANTINE` crees.
- [ ] Droits d'ecriture pour les utilisateurs Sage et pour le compte du service.
- [ ] Partage reseau en UNC, pas en lecteur mappe, si applicable.
- [ ] Test dry-run OK.
- [ ] Test reel OK sur une facture de recette.
- [ ] `/print` ne montre pas d'erreur residuelle.
- [ ] Utilisateurs formes : nom du PDF = numero de facture, depot dans `IN`.
- [ ] Politique `on_uncertified` validee avec le client.
- [ ] Procedure de purge/archivage de `OUT`, `DONE` et `QUARANTINE`.
- [ ] Kill-switch `/sites` teste.
