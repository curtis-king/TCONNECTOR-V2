# Solution — Statut SFEC cohérent malgré une écriture Sage en attente

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/checking/suivi/`
> **Statut** : IMPLÉMENTÉ — 2026-10-29
> **Branche** : `update/alu-congo`
> **Périmètre** : conservation de la certification SFEC dans les statuts
> locaux et affichage unifié `/billing` / `/invoices`.

---

## 1. Symptôme

La facture `FCDFG016` était certifiée sur SFEC, mais :

- `/billing` affichait **« A surveiller »** ;
- `/invoices` affichait **« En cours »** ;
- Sage contenait encore `SFEC_STATUT = A_SURVEILLER` et
  `SFEC_NUM_CERTIF = NULL`.

La certification était pourtant présente dans :

- `data/sfec-certified.json` ;
- la table `sfec_pending_writes` ;
- partiellement dans SQLite (`sfec_id` et `sfec_date_certif`).

---

## 2. Cause racine

1. La certification SFEC réussit et la certification est mise en file pour
   écriture dans Sage.
2. L’écriture Sage échoue car l’enregistrement est verrouillé :

   ```text
   SQL Server 80003 : Cet élément est en cours d'utilisation !
   ```

3. Un pull Sage suivant récupère les champs SFEC encore vides dans Sage.
4. `sync_sage_invoice_to_sqlite()` réécrit ces valeurs vides dans SQLite,
   y compris `sfec_statut = A_SURVEILLER` et `sfec_num_certif = ''`.
5. SQLite conserve `sfec_id` et `sfec_date_certif`, mais perd la signature et
   le numéro de certification : l’état devient hybride.
6. `/billing` lit SQLite et affiche « A surveiller » ; `/invoices` lit le cache
   Sage et peut afficher « En cours ». Les deux pages divergent.

Le problème n’est donc pas la certification SFEC elle-même, mais la
**réécriture du miroir local par un pull Sage alors qu’une écriture est encore
en attente**.

---

## 3. Règles retenues

- Une certification SFEC connue localement ne doit **jamais** être remplacée
  par des champs Sage vides.
- Une écriture Sage en attente ne doit pas transformer une facture certifiée
  en anomalie `A_SURVEILLER`.
- Les pages `/billing` et `/invoices` doivent afficher le même état effectif.
- Le libellé doit distinguer :
  - **Certifiée** : certification écrite dans Sage ;
  - **Certifiée SFEC — écriture Sage en attente** : certification SFEC valide,
    file d’écriture Sage encore active ;
  - **A surveiller** : anomalie réelle sans preuve de certification.
- Aucune donnée Sage n’est corrigée manuellement par cette évolution.

---

## 4. Modifications

### 4.1 `app/storage/db.py`

Ajout de :

- `get_pending_sfec_write(invoice_id="", numero="")` : retourne la ligne de
  `sfec_pending_writes` correspondant à une facture ;
- `get_sfec_states_by_numeros(numeros)` : retourne l’état SFEC effectif par
  numéro de facture, en combinant :
  - la preuve locale SQLite (`sfec_num_certif`, `sfec_signature`, `sfec_id`,
    `sfec_date_certif`) ;
  - la certification en attente d’écriture Sage.

### 4.2 `app/integration/sage/writer.py`

`sync_sage_invoice_to_sqlite()` :

- lit l’état SFEC local avant mise à jour ;
- si une certification locale ou une écriture en attente existe, et que Sage
  ne fournit pas encore de numéro de certification :
  - conserve le statut `CERTIFIE` ;
  - conserve ou hydrate `sfec_num_certif`, `sfec_signature`, `sfec_qr_code`
    et `sfec_date_certif` depuis la preuve locale ou la file d’attente ;
- ne remplace donc plus une certification connue par
  `A_SURVEILLER` + champs vides.

### 4.3 `app/integration/sfec/pending_writes.py`

- Une certification déjà obtenue n’est plus abandonnée après `MAX_ATTEMPTS`.
- Le nombre de tentatives reste suivi pour l’exploitation.
- Aucun `mark_to_monitor()` n’est déclenché pour une certification valide dont
  seule l’écriture Sage est bloquée.

### 4.4 `app/sync/engine.py`

- Une écriture Sage différée conserve `sfec_statut = CERTIFIE` et positionne
  `sfec_pending_sage = True` dans le cache.
- Le callback de rejeu réussi remet `sfec_pending_sage = False`, met à jour le
  numéro de certification et le statut `CERTIFIE`.
- Les branches de certification qui forçaient `EN_COURS` après mise en file
  conservent désormais l’état certifié avec le drapeau d’attente Sage.

### 4.5 Affichage

- `/api/invoices/list` enrichit chaque facture avec l’état SFEC effectif et
  `sfec_pending_sage`.
- La liste `/billing` affiche :
  - « Certifiée SFEC — Sage en attente » si une écriture est en attente ;
  - « Certifiée » sinon.
- `/invoices` applique la même surcouche locale avant rendu, au lieu de lire
  uniquement le cache Sage.

---

## 5. Tests

Fichier ajouté : `tests/test_sfec_status_consistency.py`.

Cas couverts :

- pull Sage avec champs vides ne remplace pas une certification locale ;
- pull Sage avec champs vides hydrate depuis `sfec_pending_writes` ;
- `flush()` ne downgrade pas après `MAX_ATTEMPTS` ;
- `/api/invoices/list` expose `sfec_pending_sage` et le numéro de
  certification ;
- `/invoices` affiche « Certifiée SFEC — Sage en attente » pour une
  certification en file ;
- après rejeu réussi, le cache repasse à `CERTIFIE` sans drapeau d’attente.

Commande de validation :

```bash
cd connecteur-python
.venv/bin/python -m pytest tests -q --ignore=tests/perf \
  -m "not sandbox and not integration"
```

Résultat : **307 tests passés**. Les tests sandbox réseau ne sont pas
exécutables sans accès DNS à `sandbox.api.sfec.gouv.cg`.

---

## 6. Procédure d’exploitation

Pour une facture déjà certifiée mais bloquée par un verrou Sage :

1. fermer la facture ou la session Sage qui verrouille l’enregistrement ;
2. relancer une synchronisation ou redémarrer le connecteur ;
3. laisser `sfec_pending_writes` rejouer l’écriture ;
4. vérifier dans Sage :

```sql
SELECT SFEC_STATUT, SFEC_NUM_CERTIF, SFEC_DATE_CERTIF
FROM F_DOCENTETE
WHERE DO_Piece = 'FCDFG016';
```

Attendu :

```text
SFEC_STATUT     = CERTIFIE
SFEC_NUM_CERTIF = 7378C77ABEE2A59402C6
```

Ne pas supprimer la ligne `sfec_pending_writes` ni réinitialiser le statut
manuellement : la certification doit être écrite dans Sage par le circuit
normal.
