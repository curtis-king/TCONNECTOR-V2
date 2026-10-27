# Plan — Pull Sage limité à 7 jours + sync complète manuelle depuis `/billing`

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/new-structure/`
> **Statut** : IMPLÉMENTÉ — 2026-10-27.
> **Date** : 2026-10-26 (mise à jour 2026-10-27)
> **Périmètre** : pull automatique de la base Sage (sync bidirectionnelle), vue `/billing`.
> **Hors périmètre** : calculs fiscaux SFEC, push Sage, schéma SQLite, service Windows.

---

## 1. Objectif

Le client possède plus de 10 000 factures. Le pull automatique actuel relit **toutes** les factures Sage à chaque cycle, puis fait un `SELECT` + `UPDATE` local par facture.

Décisions validées avec l'utilisateur :

1. Le pull automatique ne doit remonter que les **7 derniers jours**.
2. La base locale sera initialisée/backfillée par une **sync manuelle complète** déclenchée depuis `/billing`.
3. La fenêtre doit être configurable dans `config.json` **et** dans la page web de configuration.
4. La vue réellement concernée par l'action manuelle est **`/billing`**.
5. L'intervalle de 5 minutes déjà appliqué sur le service Windows n'est pas modifié par ce plan.

---

## 2. Constat actuel

### 2.1 Le pull complet ne filtre pas par date

`app/integration/sage/writer.py:226-310`

```sql
WHERE d.DO_Domaine = 0 AND d.DO_Type IN (6, 7)
ORDER BY d.DO_Date DESC
```

Aucune clause de date : toutes les factures sont lues à chaque appel.

`app/sync/bidirectional.py:368-399`

```python
def pull_from_sage():
    sage_invoices = read_invoices_from_sage()
    ...
    for sage_inv in sage_invoices:
        with get_cursor() as cur:
            cur.execute("SELECT id FROM invoices WHERE numero = ?", (numero,))
        ...
        _update_from_sage(existing["id"], sage_inv)
```

Conséquence : pour `N` factures, environ `2N` requêtes SQLite et `N` `UPDATE` par cycle.

### 2.2 Le pull incrémental SFEC est déjà borné

`app/integration/sage/database.py:795-805` utilise déjà `updated_from` avec :

```sql
AND (d.DO_Date >= ? OR d.cbModification >= ?)
```

Ce plan reprend le même principe pour le pull complet, mais avec une fenêtre glissante de `N` jours.

### 2.3 La vue `/billing` déclenche déjà une sync manuelle

`app/web/templates/billing/list.html:29-32`

```html
<button class="btn btn-success" onclick="syncBi(this)" style="margin-left:8px">Sync Sage</button>
```

`app/web/templates/billing/list.html:154`

```javascript
function syncBi(btn){ ... api("POST","/api/sync/bi",{}) ... }
```

`app/web/routes/sync_api.py:131-138`

```python
@bp.route("/api/sync/bi", methods=["POST"])
@_login_required
def api_sync_bi():
    try:
        result = sync_bidirectional.full_sync()
        return jsonify({"success": True, **result})
```

Aujourd'hui l'appel est **synchrone** : avec 10 000 factures, la requête HTTP peut durer plusieurs minutes et expirer côté navigateur/proxy.

---

## 3. Comportement cible

### 3.1 Fenêtre automatique

- Clé de configuration : `db.sage_pull_days`.
- Valeur par défaut : `7`.
- `0` = aucune limite (pull complet).
- `N > 0` = factures dont `DO_Date >= aujourd'hui - N jours` **ou** `cbModification >= aujourd'hui - N jours`.
- Le filtre est appliqué **dans la requête SQL Sage**, pas après chargement en Python.
- Si `cbModification` n'existe pas dans `F_DOCENTETE`, repli sur `DO_Date` seul.

### 3.2 Sync manuelle complète

- Bouton dans `/billing` : **« Sync complète »**.
- Appelle `POST /api/sync/bi` avec `{"force_all": true}`.
- `force_all=true` ignore `sage_pull_days` et remonte toutes les factures.
- L'appel est exécuté en arrière-plan pour éviter un timeout HTTP.
- Le bouton affiche l'état : en cours / terminé / erreur.
- Le bouton existant « Sync Sage » devient « Sync Sage (7 j) » et utilise la fenêtre automatique.

### 3.3 Configuration web

Dans l'onglet **Synchronisation** de `/config` :

```text
Fenêtre pull Sage (jours) : [ 7 ]
0 = toutes les factures ; 7 = 7 derniers jours
```

Validation : entier entre `0` et `3650`.

---

## 4. Modifications détaillées

### 4.1 `app/config/manager.py`

**Bloc à remplacer** (`app/config/manager.py:62-63`) :

```python
            "polling_interval_ms": 30000,
            "polling_enabled": True,
```

**Bloc corrigé** :

```python
            "polling_interval_ms": 30000,
            "polling_enabled": True,
            "sage_pull_days": 7,
```

Aucun autre changement dans ce fichier.

---

### 4.2 `app/integration/sage/writer.py`

**Bloc à remplacer** (`app/integration/sage/writer.py:226`) :

```python
def read_invoices_from_sage():
```

**Bloc corrigé** :

```python
def read_invoices_from_sage(sage_pull_days=None, force_all=False):
```

Ajouter le calcul de la date et la détection de `cbModification` avant la construction de `query`.

**Bloc à remplacer** (`app/integration/sage/writer.py:236-240`) :

```python
            cur.execute("""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = 'F_DOCENTETE' AND COLUMN_NAME LIKE 'SFEC%'
            """)
            sfec_cols_present = {row[0] for row in cur.fetchall()}
```

**Bloc corrigé** :

```python
            cur.execute("""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = 'F_DOCENTETE'
                  AND (COLUMN_NAME LIKE 'SFEC%' OR COLUMN_NAME = 'cbModification')
            """)
            doc_cols = {str(row[0]) for row in cur.fetchall()}
            doc_cols_lower = {col.lower(): col for col in doc_cols}
            sfec_cols_present = {
                col for lower, col in doc_cols_lower.items() if lower.startswith("sfec")
            }
            has_cb_modification = "cbmodification" in doc_cols_lower
```

**Bloc à remplacer** (`app/integration/sage/writer.py:271-309`) :

```python
            query = f"""
                ...
                WHERE d.DO_Domaine = 0 AND d.DO_Type IN (6, 7)
                ORDER BY d.DO_Date DESC
            """
            cur.execute(query)
```

**Bloc corrigé** :

```python
            date_filter = ""
            params = []
            try:
                days = int(sage_pull_days or 0)
            except (TypeError, ValueError):
                days = 0

            if not force_all and days > 0:
                since = datetime.now() - timedelta(days=days)
                if has_cb_modification:
                    date_filter = " AND (d.DO_Date >= ? OR d.cbModification >= ?)"
                    params = [since, since]
                else:
                    date_filter = " AND d.DO_Date >= ?"
                    params = [since]

            query = f"""
                ...
                WHERE d.DO_Domaine = 0 AND d.DO_Type IN (6, 7)
                {date_filter}
                ORDER BY d.DO_Date DESC
            """
            cur.execute(query, params)
```

Imports à ajouter en tête de `writer.py` :

```python
from datetime import datetime, timedelta
```

Le reste du traitement (mapping, lignes, retours) reste inchangé.

---

### 4.3 `app/sync/bidirectional.py`

#### 4.3.1 Fenêtre de pull

**Bloc à remplacer** (`app/sync/bidirectional.py:368-371`) :

```python
def pull_from_sage():
    sage_invoices = read_invoices_from_sage()
    if not sage_invoices:
        return {"pulled": 0, "new": 0, "updated": 0}
```

**Bloc corrigé** :

```python
def pull_from_sage(force_all=False):
    from app.config.manager import get_config
    db_cfg = get_config().get("db", {}) or {}
    try:
        pull_days = int(db_cfg.get("sage_pull_days", 7) or 0)
    except (TypeError, ValueError):
        pull_days = 7

    logger.info(
        "Pull Sage: fenetre=%s jours%s",
        "complete" if force_all else pull_days,
        " (force_all)" if force_all else "",
    )
    sage_invoices = read_invoices_from_sage(
        sage_pull_days=pull_days,
        force_all=force_all,
    )
    if not sage_invoices:
        return {"pulled": 0, "new": 0, "updated": 0}
```

#### 4.3.2 Sync complète

**Bloc à remplacer** (`app/sync/bidirectional.py:512-517`) :

```python
def full_sync():
    logger.info("Demarrage sync bidirectionnelle...")
    cert_result = certify_pending_pos_invoices()
    contact_result = push_contacts_to_sage()
    push_result = push_to_sage()
    pull_result = pull_from_sage()
```

**Bloc corrigé** :

```python
def full_sync(force_all=False):
    logger.info("Demarrage sync bidirectionnelle...")
    cert_result = certify_pending_pos_invoices()
    contact_result = push_contacts_to_sage()
    push_result = push_to_sage()
    pull_result = pull_from_sage(force_all=force_all)
```

Le reste de `full_sync()` reste identique.

#### 4.3.3 Sync manuelle asynchrone

Ajouter un état de synchronisation manuelle, protégé par `_sync_lock` :

```python
_manual_sync_thread = None
_manual_sync_state = {
    "running": False,
    "mode": "",
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "last_error": None,
}
```

Ajouter :

```python
def is_sync_running():
    with _sync_lock:
        return bool(_manual_sync_state["running"])


def get_sync_stats():
    with _sync_lock:
        stats = dict(_sync_stats)
        stats.update({
            "running": _manual_sync_state["running"],
            "mode": _manual_sync_state["mode"],
            "started_at": _manual_sync_state["started_at"],
            "finished_at": _manual_sync_state["finished_at"],
            "last_result": _manual_sync_state["last_result"],
            "last_error": _manual_sync_state["last_error"],
        })
        return stats


def start_manual_sync(force_all=False):
    global _manual_sync_thread

    with _sync_lock:
        if _manual_sync_state["running"]:
            return {"success": False, "error": "Une synchronisation est deja en cours"}
        _manual_sync_state.update({
            "running": True,
            "mode": "full" if force_all else "window",
            "started_at": datetime.utcnow().isoformat() + "Z",
            "finished_at": None,
            "last_result": None,
            "last_error": None,
        })
        _manual_sync_thread = threading.Thread(
            target=_run_manual_sync,
            args=(force_all,),
            daemon=True,
            name="bi-sync-manual",
        )
        _manual_sync_thread.start()

    return {
        "success": True,
        "started": True,
        "mode": "full" if force_all else "window",
    }


def _run_manual_sync(force_all):
    try:
        result = full_sync(force_all=force_all)
        with _sync_lock:
            _manual_sync_state["last_result"] = result
    except Exception as e:
        logger.error("Erreur sync manuelle: %s", e)
        with _sync_lock:
            _manual_sync_state["last_error"] = str(e)[:300]
    finally:
        with _sync_lock:
            _manual_sync_state["running"] = False
            _manual_sync_state["finished_at"] = datetime.utcnow().isoformat() + "Z"
```

Dans `_bi_sync_loop`, éviter de lancer le cycle automatique pendant une sync manuelle :

```python
def _bi_sync_loop(interval):
    while not _stop_event.is_set():
        if not is_sync_running():
            try:
                full_sync()
            except Exception as e:
                logger.error("Erreur sync bidirectionnelle: %s", e)
                with _sync_lock:
                    _sync_stats["errors"] += 1
        _stop_event.wait(interval)
```

> Le verrou protège l'état, pas l'exécution concurrente. La condition `is_sync_running()` évite le chevauchement manuel/automatique sans bloquer le thread.

---

### 4.4 `app/web/routes/sync_api.py`

**Bloc à remplacer** (`app/web/routes/sync_api.py:131-138`) :

```python
@bp.route("/api/sync/bi", methods=["POST"])
@_login_required
def api_sync_bi():
    try:
        result = sync_bidirectional.full_sync()
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
```

**Bloc corrigé** :

```python
@bp.route("/api/sync/bi", methods=["POST"])
@_login_required
def api_sync_bi():
    data = request.get_json(silent=True) or {}
    force_all = bool(data.get("force_all", False))
    result = sync_bidirectional.start_manual_sync(force_all=force_all)
    status = 202 if result.get("success") else 409
    return jsonify(result), status
```

`request` est déjà importé dans ce module.

`GET /api/sync/bi/stats` renvoie déjà `get_sync_stats()` ; il exposera automatiquement `running`, `mode`, `last_result`, `last_error`.

---

### 4.5 `app/web/routes/config.py`

**Contexte du template** (`app/web/routes/config.py:140-142`) :

```python
        pe_t=_s(db.get("polling_enabled"), True),
        pe_f=_s(db.get("polling_enabled"), False),
        poll_ms=db.get("polling_interval_ms", 30000),
```

**Bloc corrigé** :

```python
        pe_t=_s(db.get("polling_enabled"), True),
        pe_f=_s(db.get("polling_enabled"), False),
        poll_ms=db.get("polling_interval_ms", 30000),
        sage_pull_days=db.get("sage_pull_days", 7),
```

**Validation API** (`app/web/routes/config.py:496-500`) :

```python
    db = dict(cfg.get("db", {}))
    if "polling_enabled" in data:
        db["polling_enabled"] = _bool(data["polling_enabled"])
    errors = []
    _apply_bounded(data, db, (("polling_interval_ms", 1000, 3600000),), errors)
```

**Bloc corrigé** :

```python
    db = dict(cfg.get("db", {}))
    if "polling_enabled" in data:
        db["polling_enabled"] = _bool(data["polling_enabled"])
    errors = []
    _apply_bounded(data, db, (
        ("polling_interval_ms", 1000, 3600000),
        ("sage_pull_days", 0, 3650),
    ), errors)
```

---

### 4.6 `app/web/templates/config/index.html`

Dans l'onglet **Synchronisation**, après le champ `polling_interval_ms` (`app/web/templates/config/index.html:99`) :

```html
<div>
  <label>Fenêtre pull Sage (jours)</label>
  <input type="number" name="sage_pull_days" value="{{ sage_pull_days }}" min="0" max="3650">
  <div class="desc">0 = toutes les factures ; 7 = 7 derniers jours</div>
</div>
```

Le formulaire `form-sync` envoie déjà tous ses champs à `/api/config/sync`.

---

### 4.7 `config.example.json`

Ajouter dans `db` :

```json
"sage_pull_days": 7
```

Et sur le serveur client, ajouter la même clé dans `config.json`. Si la clé est absente, le code utilisera `7` par défaut.

---

## 5. Vue `/billing`

### 5.1 Contexte serveur

`app/web/routes/billing.py:24-46` : ajouter la fenêtre courante pour l'affichage.

**Bloc à remplacer** :

```python
    stats = invoice_engine.count_invoices()
    bi_stats = sync_bidirectional.get_sync_stats()
```

**Bloc corrigé** :

```python
    from app.config.manager import get_config
    stats = invoice_engine.count_invoices()
    bi_stats = sync_bidirectional.get_sync_stats()
    sage_pull_days = get_config().get("db", {}).get("sage_pull_days", 7)
```

Puis passer `sage_pull_days=sage_pull_days` au template.

### 5.2 Boutons

`app/web/templates/billing/list.html:29-32`

**Bloc à remplacer** :

```html
<div class="card no-print"><h2>Actions</h2>
<a href="/billing/invoice/new" class="btn btn-primary">Nouvelle Facture</a>
<button class="btn btn-success" onclick="syncBi(this)" style="margin-left:8px">Sync Sage</button>
<span style="margin-left:8px;font-size:12px;color:#64748b">Derniere sync: {{ last_sync }}</span>
</div>
```

**Bloc corrigé** :

```html
<div class="card no-print"><h2>Actions</h2>
<a href="/billing/invoice/new" class="btn btn-primary">Nouvelle Facture</a>
<button class="btn btn-success" onclick="syncBi(this, false)" style="margin-left:8px">
  Sync Sage ({{ sage_pull_days }} j)
</button>
<button class="btn btn-warning" onclick="syncBi(this, true)" style="margin-left:8px">
  Sync complète
</button>
<span style="margin-left:8px;font-size:12px;color:#64748b">
  Derniere sync: {{ last_sync }} · Pull auto: {{ sage_pull_days }} jours
</span>
</div>
```

### 5.3 JavaScript

`app/web/templates/billing/list.html:154`

**Bloc à remplacer** :

```javascript
function syncBi(btn){if(btn){btn.disabled=true;btn.textContent="Sync en cours...";}api("POST","/api/sync/bi",{}).then(function(d){if(d&&d.success===false){showToast("Sync: "+(d.error||"echec"),"err");return}showToast("Sync: "+(d.pushed||0)+" poussees, "+(d.pull_new||0)+" tirees","ok");render()}).catch(function(e){showToast("Erreur sync: "+e,"err")}).finally(function(){if(btn){btn.disabled=false;btn.textContent="Sync Sage";}});}
```

**Bloc corrigé** :

```javascript
function syncBi(btn, full){
  if(full && !confirm("Sync complète : toutes les factures Sage seront relues. Cela peut prendre plusieurs minutes. Continuer ?")) return;
  var label = full ? "Sync complète" : "Sync Sage";
  if(btn){btn.disabled=true;btn.textContent=full?"Sync complète en cours...":"Sync en cours...";}
  api("POST","/api/sync/bi",{force_all:!!full}).then(function(d){
    if(d&&d.success===false){showToast("Sync: "+(d.error||"echec"),"err");return;}
    showToast(full?"Sync complète lancée":"Sync lancée","info");
    pollSyncBi(btn, label);
  }).catch(function(e){
    showToast("Erreur sync: "+e,"err");
    if(btn){btn.disabled=false;btn.textContent=label;}
  });
}
function pollSyncBi(btn, label){
  var attempts=0;
  var timer=setInterval(function(){
    attempts++;
    api("GET","/api/sync/bi/stats").then(function(s){
      if(s && s.running){
        if(attempts>=150){clearInterval(timer);showToast("Sync toujours en cours, vérifiez plus tard","warn");if(btn){btn.disabled=false;btn.textContent=label;}return;}
        return;
      }
      clearInterval(timer);
      if(s && s.last_error){showToast("Sync: "+s.last_error,"err");}
      else{showToast("Sync terminée","ok");render();}
      if(btn){btn.disabled=false;btn.textContent=label;}
    }).catch(function(){
      if(attempts>=150){clearInterval(timer);if(btn){btn.disabled=false;btn.textContent=label;}}
    });
  },2000);
}
```

> Le bouton « Sync complète » est protégé par `@_login_required` et le CSRF existant (`api()` dans `app.js`).

---

## 6. Tests

### 6.1 Nouveaux tests backend

Fichier : `tests/test_sage_pull_window.py`

Cas à couvrir :

1. `sage_pull_days=0` → aucune clause de date.
2. `sage_pull_days=7` → clause `d.DO_Date >= ?` et paramètre proche de `now - 7 jours`.
3. `cbModification` présent → clause `(d.DO_Date >= ? OR d.cbModification >= ?)`.
4. `force_all=True` → aucune clause, même avec `sage_pull_days=7`.
5. `pull_from_sage()` lit `db.sage_pull_days` et le transmet.
6. Valeur absente → défaut `7`.
7. Valeur invalide (`"abc"`, `None`, négative) → repli sûr, pas d'exception.

### 6.2 Tests configuration

Fichier : `tests/test_config_sync_api.py` (ou compléter un test existant).

- `POST /api/config/sync` avec `sage_pull_days=7` → `200`, valeur persistée.
- `sage_pull_days=-1` → `400`.
- `sage_pull_days=3651` → `400`.
- `sage_pull_days=0` → `200`.

### 6.3 Tests `/billing`

- La page `/billing` contient « Sync complète » et « Pull auto: 7 jours ».
- `POST /api/sync/bi` avec `{"force_all": true}` → `202`, `started=true`.
- Un deuxième appel pendant une sync → `409`.
- `GET /api/sync/bi/stats` expose `running`, `mode`, `last_result`.

### 6.4 Non-régression

- `tests/test_mirror_totals.py` : `read_invoices_from_sage()` sans argument doit continuer à fonctionner.
- Snapshots `tests/snapshots/040_GET_config.json` et `007_GET_api_config.json` à mettre à jour si le rendu/config change.
- Aucun test ne doit toucher la base de production : utiliser `TCONNECTOR_DB_PATH` temporaire.

---

## 7. Critères de validation

1. `pytest` ciblé vert :
   ```bash
   pytest tests/test_sage_pull_window.py tests/test_config_sync_api.py tests/test_mirror_totals.py
   ```
2. Avec `sage_pull_days=7`, une facture Sage datée de 10 jours n'est **pas** remontée ; une facture de 2 jours l'est.
3. Une facture ancienne mais avec `cbModification` récente est remontée.
4. `force_all=true` remonte les 10 000 factures (test avec mock, pas la base client).
5. `/config` affiche et sauvegarde la fenêtre.
6. `/billing` affiche les deux boutons et l'état de sync.
7. Un clic sur « Sync complète » ne bloque pas la requête HTTP : réponse `202`, puis polling de `/api/sync/bi/stats`.
8. Aucun changement de comportement sur le push Sage, la certification SFEC et les totaux.

---

## 8. Risques et parades

| Risque | Parade |
|---|---|
| Facture ancienne modifiée récemment non remontée | Filtre `DO_Date OR cbModification` |
| `cbModification` absent de Sage | Repli sur `DO_Date` seul |
| Base locale vide au premier lancement | Sync complète manuelle depuis `/billing` |
| Avoirs liés à une facture parente hors fenêtre | Sync complète manuelle ou fenêtre plus large |
| Statut SFEC d'une ancienne facture non rafraîchi | Réconciliation SFEC existante ou sync complète |
| Full sync de 10 000 factures trop long en HTTP | Exécution asynchrone + polling stats |
| Chevauchement sync auto/manuelle | `is_sync_running()` + état protégé |
| Requête Sage lente | Vérifier index sur `F_DOCENTETE.DO_Date` et `cbModification` |
| `sage_pull_days` invalide | Repli sur `7`, validation web `0..3650` |
| Timezone | Fenêtre calculée côté serveur avec `datetime.now()` ; à valider sur le serveur client |

---

## 9. Ordre d'implémentation proposé

### Commit 1 — Fenêtre de pull configurable

1. `app/config/manager.py` : défaut `sage_pull_days=7`.
2. `app/integration/sage/writer.py` : filtre SQL + `force_all`.
3. `app/sync/bidirectional.py` : `pull_from_sage(force_all)`.
4. `app/web/routes/config.py` + template : champ web.
5. `config.example.json`.
6. Tests backend/config.

**Critère** : pull automatique limité à 7 jours, tests verts.

### Commit 2 — Sync complète manuelle depuis `/billing`

1. `app/sync/bidirectional.py` : état + `start_manual_sync`.
2. `app/web/routes/sync_api.py` : `force_all`, réponse `202`.
3. `app/web/routes/billing.py` + `list.html` : boutons, polling, affichage.
4. Tests `/billing` et API.

**Critère** : sync complète asynchrone fonctionnelle, pas de timeout HTTP.

> Chaque commit est soumis à validation avant exécution. Aucun staging global.

---

## 10. Ce que ce plan ne fait pas

- Ne modifie pas `polling_interval_ms` ni le service Windows.
- Ne modifie pas la logique de push Sage.
- Ne modifie pas les calculs SFEC, la TVA, les centimes ou les tolérances.
- Ne modifie pas le schéma SQLite.
- Ne touche pas aux données BIJOU.
- Ne supprime pas les factures anciennes du miroir local.

---

## 11. Décisions à valider

1. Fenêtre par défaut : **7 jours** (confirmé).
2. Sync complète manuelle depuis `/billing` : **bouton séparé** avec confirmation.
3. Exécution asynchrone de la sync complète : **oui**, pour éviter le timeout HTTP.
4. `sage_pull_days=0` : **pull complet** (utile pour un backfill ponctuel).
5. Le bouton « Sync Sage (7 j) » reste un déclenchement manuel de la fenêtre automatique.
