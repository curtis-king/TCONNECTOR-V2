# Rapport d'analyse — Fichiers hors `app/` : quoi supprimer, quoi garder

> **Projet** : `connecteur-python`
> **Date** : 09/09/2026
> **Objet** : Répondre à la question « puis-je supprimer tous les fichiers hors de `app/`, vu que tout est dans `app/` ? ».
> **Méthode** : recherche statique exhaustive (grep de tous les imports Python, y compris tardifs et dynamiques, `.bat`, `.iss`, `.md`, chaînes) sur `app/`, `main.py`, `scripts/` et les scripts Windows.

---

## 1. Verdict court

**Non, pas tous.** Mais **15 des 18 modules `*.py` racine sont de simples passerelles à présent orphelines** (plus rien ne les importe) et peuvent être supprimés. **2 passerelles sont encore référencées** (par des scripts) et **`main.py` + les fichiers de config/requirements sont indispensables**.

---

## 2. Classification détaillée

### 2.1 Passerelles NON référencées → supprimables (15 fichiers)

Aucun endroit du code (ni `app/`, ni `main.py`, ni `scripts/`, ni les `.bat`) n'importe plus ces modules. Ils ne sont que des ré-exporteurs vers `app/` :

| Fichier racine | Cible réellement utilisée | Statut |
|---|---|---|
| `database.py` | `app.integration.sage.database` | ✅ supprimable |
| `sqlite_db.py` | `app.storage.db` | ✅ supprimable |
| `sfec_client.py` | `app.integration.sfec.client` | ✅ supprimable |
| `sfec_endpoints.py` | `app.integration.sfec.endpoints` | ✅ supprimable |
| `connectivity.py` | `app.sync.connectivity` | ✅ supprimable |
| `sync_engine.py` | `app.sync.engine` | ✅ supprimable |
| `sync_bidirectional.py` | `app.sync.bidirectional` | ✅ supprimable |
| `article_sync.py` | `app.sync.article_sync` | ✅ supprimable |
| `invoice_engine.py` | `app.domain.invoices` | ✅ supprimable |
| `pos_engine.py` | `app.domain.pos` | ✅ supprimable |
| `pdf_generator.py` | `app.domain.pdf` | ✅ supprimable |
| `seed_data.py` | `app.domain.seed` | ✅ supprimable |
| `sage_writer.py` | `app.integration.sage.writer` | ✅ supprimable |
| `user_auth.py` | `app.web.auth.user_auth` | ✅ supprimable |
| `dashboard.py` | `app.web.dashboard` | ✅ supprimable |

> Vérification : `grep -rE "^(from|import) <module>" app/ main.py scripts/` → **zéro résultat** pour ces 15 noms. Les seules occurrences restantes sont dans des docstrings / chaînes (faux positifs).

### 2.2 Passerelles ENCORE référencées → à adapter AVANT suppression (2 fichiers)

| Fichier | Quoi y référence | Adaptation nécessaire |
|---|---|---|
| `service.py` | `scripts/windows/_install_svc.py` (`from service import TConnectorService`) + `scripts/windows/install_service.bat` lignes 80-87 (régénère un `_install_svc.py`), ligne 83 (`from service import TConnectorService`) | Remplacer par `from app.service.windows_service import TConnectorService` dans les 2 emplacements |
| `config_manager.py` | `scripts/generate_articles.py` ligne 26 (`from config_manager import get_db_config`) | Remplacer par `from app.config.manager import get_db_config` |

> `main.py` n'importe **plus aucune** passerelle : il utilise directement `app.config.manager`, `app.integration.sage.database`, `app.sync.connectivity`, `app.sync.scheduler`, `app.web.app`, etc. (vérifié ligne par ligne).

### 2.3 Indispensables — jamais supprimer

| Fichier / dossier | Rôle |
|---|---|
| `main.py` | Point d'entrée de l'application (`python main.py`) |
| `requirements.txt` / `requirements-windows.txt` / `requirements-linux.txt` | Dépendances par plateforme (référencés par `install_service.bat`, `start.bat`) |
| `config.json` | Configuration runtime réelle (lu par `app.config.manager`) |
| `config.example.json` | Modèle de configuration |
| `app/` | Le nouveau code restructuré |
| `docs/`, `scripts/`, `tests/` | Documentation, scripts de déploiement, emplacement futur des tests |
| `data/` | Données runtime (SQLite, logs) — déjà dans `.gitignore` |

### 2.4 Fichiers optionnels (à votre appréciation)

| Fichier | Situation |
|---|---|
| `generate_articles.py` (racine) | **Launcher** de `scripts/generate_articles.py` : personne ne l'appelle (ni .bat, ni main, ni app), mais il sert de raccourci CLI (`python generate_articles.py`). Supprimable si ce raccourci n'est plus utile ; sinon le garder. |
| `test_invoice.pdf`, `test_ticket.pdf` | Exemples de sortie PDF, **non référencés** dans le code. Supprimables (ou à déplacer dans `docs/` pour référence). |
| `__pycache__/` | Généré par Python, ignoré par git — ne pas commit, ne pas supprimer à la main nécessairement. |

---

## 3. Suppression effective (une fois les 2 adaptations faites)

```bash
# 1. Adapter les 2 scripts qui dépendent des passerelles restantes
sed -i 's/from service import TConnectorService/from app.service.windows_service import TConnectorService/' \
    scripts/windows/_install_svc.py
sed -i 's/from config_manager import get_db_config/from app.config.manager import get_db_config/' \
    scripts/generate_articles.py

# 2. Adapter le .bat qui régénère _install_svc.py (ligne 83)
# dans scripts/windows/install_service.bat : remplacer
#   echo from service import TConnectorService >> "%ROOT%\_install_svc.py"
# par
#   echo from app.service.windows_service import TConnectorService >> "%ROOT%\_install_svc.py"

# 3. Supprimer les 15 passerelles orphelines
rm database.py sqlite_db.py sfec_client.py sfec_endpoints.py connectivity.py \
   sync_engine.py sync_bidirectional.py article_sync.py invoice_engine.py \
   pos_engine.py pdf_generator.py seed_data.py sage_writer.py user_auth.py dashboard.py

# 4. (Optionnel) supprimer les fichiers non référencés
rm generate_articles.py test_invoice.pdf test_ticket.pdf
```

---

## 4. Risques & prérequis avant de supprimer

⚠️ **Recommandation forte : ne pas supprimer tant que l'application n'a pas été exécutée au moins une fois** avec les dépendances installées.

- L'environnement actuel n'a **pas** `pip`/`venv` → `flask`, `pyodbc`, `pywin32` absents → aucune exécution réelle possible pour l'instant.
- Les passerelles servent de **filet de sécurité** : leur suppression est justifiée par le grep statique (zéro importeur), mais une validation d'exécution (`python main.py`, import de chaque module) reste le test définitif.
- Après suppression, si un jour un script externe (ou un notebook) faisait `import database` à la racine, il faudrait l'adapter vers `app.integration.sage.database`.

### Chemin recommandé

1. Installer un environnement : `sudo apt install python3.14-venv && python3 -m venv .venv && .venv/bin/pip install -r requirements-linux.txt`
2. Lancer `python main.py` et vérifier que le dashboard répond sur `:3000`.
3. Adapter les 2 scripts (§ 2.2) + le `.bat`.
4. Supprimer les 15 passerelles + (optionnel) § 2.4.
5. Relancer `python main.py` et les imports pour confirmer aucun `ModuleNotFoundError`.
6. Committer la suppression (un commit dédié est plus propre).

---

## 5. Conclusion

- **15 modules racine** sont des passerelles orphelines → supprimables.
- **2 passerelles** (`service.py`, `config_manager.py`) → garder ou adapter d'abord.
- **`main.py`, requirements, config** → indispensables.
- La suppression totale « tout sauf `app/` » n'est **pas possible** : l'application a besoin de `main.py`, de la configuration et des dépendances déclarées à la racine.