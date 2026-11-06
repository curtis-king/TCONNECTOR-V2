# Solution — Page /pending : afficher toutes les factures A_SURVEILLER

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/checking/suivi/`
> **Statut** : IMPLÉMENTÉ — 2026-11-06
> **Branche** : `bayo/niu-individual`
> **Périmètre** : page `/pending`, motifs SFEC, statut `A_SURVEILLER`.

---

## 1. Symptôme

La page `/pending` n'affichait pas les factures dont le statut SFEC est `A_SURVEILLER` alors qu'elles étaient bien présentes dans Sage et dans le miroir SQLite.

Constat sur la base de test :

| Source | Nombre |
|---|---|
| `invoices.sfec_statut = 'A_SURVEILLER'` | 3 |
| `sfec_anomalies` (table des motifs) | 0 |

Les 3 factures étaient donc exclues de la première liste de `/pending` (qui filtre volontairement `A_SURVEILLER`) et absentes de la seconde liste (« Factures à surveiller », lue uniquement depuis `sfec_anomalies`).

---

## 2. Cause racine

La page `/pending` comporte deux sections :

1. **« En attente de certification »** : construite depuis le cache Sage. Elle exclut explicitement les statuts `EN_COURS`, `ERREUR`, `A_SURVEILLER`, `CERTIFIE`, `DEJA_CERTIFIE`.
2. **« Factures à surveiller »** : lue **uniquement** depuis `sfec_anomalies`.

Or :

- `_update_from_sage()` (dans `app/sync/bidirectional.py`) recopie `SFEC_STATUT = 'A_SURVEILLER'` depuis Sage vers `invoices.sfec_statut`, mais **n'écrit jamais** dans `sfec_anomalies`.
- Certaines factures sont marquées `A_SURVEILLER` directement dans Sage, ou l'ont été avant l'existence de la table `sfec_anomalies`. Elles n'ont donc aucun motif enregistré.

SQLite n'est pas en cause : la requête utilise un `LIMIT 500` applicatif, pas une limite moteur.

---

## 3. Correctif implémenté

### 3.1 Nouvelle fonction de stockage

`app/storage/db.py` : ajout de `fetch_sfec_monitor_items()`.

Elle construit l'union dédupliquée (par numéro de facture) de trois sources, par ordre de priorité :

1. `sfec_anomalies` : motif connu, date de détection conservée ;
2. `invoices` avec `sfec_statut = 'A_SURVEILLER'` sans ligne d'anomalie : motif de secours « Statut SFEC : A_SURVEILLER (motif non enregistré) » ;
3. (optionnel, via la route) le cache Sage pour les factures `A_SURVEILLER` pas encore présentes dans le miroir SQLite.

Le résultat est trié par date décroissante et peut être limité.

### 3.2 Route `/pending`

`app/web/routes/dashboard.py` :

- La section « Factures à surveiller » utilise désormais `fetch_sfec_monitor_items()` au lieu de `fetch_sfec_anomalies(limit=500)`.
- Les factures `A_SURVEILLER` présentes uniquement dans le cache Sage sont ajoutées si elles n'ont pas encore de ligne SQLite.
- Pagination dédiée (`mpage`, 100 par page) : les 888 factures `A_SURVEILLER` restent toutes accessibles.
- La section « En attente de certification » est inchangée.

### 3.3 Template

`app/web/templates/dashboard/pending.html` :

- Ajout de la pagination pour la section « Factures à surveiller » (`Precedent` / `Suivant`, indique X-Y sur Z).
- Les liens de pagination de la première liste conservent le paramètre `mpage`.

---

## 4. Tests

### Nouveaux tests

- `tests/test_sfec_anomalies.py::test_page_pending_affiche_a_surveiller_sans_anomalie` : une facture `A_SURVEILLER` en SQLite sans anomalie apparaît dans la section « Factures à surveiller » avec le motif de secours.
- `tests/test_sfec_anomalies.py::test_page_pending_pagine_les_factures_a_surveiller` : 150 items → pagination 2 pages, le 2e paramètre `?mpage=2` affiche la suite.

### Test mis à jour

- `tests/test_dashboard_security.py::test_pending_rendu_echappe_et_filtre` : une facture `A_SURVEILLER` du cache Sage reste visible (elle n'est plus exclue). L'XSS est toujours échappé.

### Tests infrastructure

- `tests/conftest.py` : `admin_credentials` utilise le mot de passe connu `Test-Admin-123` quand `dashboard.login_password` est vide (la migration le vide après création du compte).
- `tests/conftest.py` : `_init_sqlite()` redéfinit le mot de passe de l'admin par défaut en base pour que le vrai flux de login fonctionne.

---

## 5. Utilisation manuelle

Accéder à `http://<connecteur>/pending`.

- Section haute : factures en cours d'attente de certification (bouton « Certifier », bouton « Surveiller »).
- Section basse : factures `A_SURVEILLER` avec motif et bouton « Relancer ».

« Relancer » remet le statut à vide (NULL) pour une seule pièce, puis tente la certification immédiate si la facture est dans le cache, sinon au prochain cycle.

---

## 6. Limites connues

- Le motif de secours est générique (« motif non enregistré »). Pour un motif précis, il faut utiliser « Relancer » ou attendre que le cycle de certification produise une erreur SFEC avec motif.
- La limite `TOP 1000` de la requête Sage (`fetch_sales_invoices`) est conservée telle quelle : elle ne concerne que le cache en mémoire. `/pending` s'appuie désormais principalement sur le miroir SQLite, qui contient l'intégralité des factures tirées par `pull_from_sage()`.
