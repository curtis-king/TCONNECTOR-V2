# Solution — NIU absent : facture traitée comme particulier

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/checking/suivi/`
> **Statut** : IMPLÉMENTÉ — 2026-11-05
> **Branche** : `bayo/niu-individual`
> **Périmètre** : construction du payload SFEC, tests de non-régression et
> logo de la page de connexion.

---

## 1. Symptôme

Les factures Sage du client partaient en certification avec :

```text
recipient_type=business
recipient_niu=absent
```

SFEC répondait :

```text
recipient_niu requis pour recipient_type=business
```

Les clients concernés sont des prospects (`CT_Type = 0`) dont les colonnes
NIU sont vides. La base du client ne contient pas la colonne `CT_NIU` ; le
connecteur lit donc `CT_Identifiant`, également vide.

Exemples :

| Code tiers | Intitulé |
|---|---|
| `414PROSSAMBA` | PROSPECTION SAMBA AMEDE |
| `414PROSPEBIENVENU` | PROSPECT BIENVENUE |
| `414PROSNZITOUKOU` | PROSPECTION NZITOUKOULOU |
| `412PROSBLAIZINA` | PROSBLAIZINA |
| `412PROSRUFIN` | PROSPECTRUFIN |

---

## 2. Cause racine

`_map_recipient_type("")` retournait `business` par défaut. Comme
`recipient_niu` était vide, `validate_sfec_payload()` refusait le payload
avant même l'appel HTTP.

Le connecteur n'avait donc aucun moyen de certifier une facture dont le
client Sage n'a pas de NIU, même lorsque ce client est manifestement un
particulier ou un prospect.

---

## 3. Règle retenue

- **NIU absent** → `recipient_type = individual`, automatiquement.
- **NIU présent mais invalide** (longueur différente de 16 ou 17) → le
  payload reste bloqué ; le NIU n'est jamais remplacé ni corrigé.
- **Client comptoir** → `individual`, comportement inchangé.
- **NIU valide** → le type mappé depuis Sage est conservé.
- Le NIU de l'entreprise (`company.tax_number`) n'est **jamais** utilisé
  comme NIU acheteur ; il reste uniquement dans `taxpayer_niu`.
- Aucune donnée client n'est modifiée dans Sage ni dans SQLite.

Un log dédupliqué est écrit par code tiers :

```text
Client sans NIU: code=414PROSSAMBA nom=PROSPECTION SAMBA AMEDE type_source=business -> individual
```

Le log du payload contient aussi le code et le nom :

```text
SFEC payload: type=individual counter=False code=414PROSSAMBA nom=PROSPECTION SAMBA AMEDE recipient_niu=absent items=1 amount=...
```

---

## 4. Modification

### `app/integration/sfec/endpoints.py`

Dans `db_invoice_to_sfec()` :

```python
if counter_customer:
    recipient_type = "individual"
elif not raw_niu:
    _log_missing_niu(code_tiers, recipient_name, recipient_type)
    recipient_type = "individual"
```

Ajout de `_log_missing_niu()` avec déduplication par code/nom et plafond de
5000 entrées pour éviter de saturer les journaux à chaque cycle.

Aucune modification de :

- `app/storage/db.py` ;
- `app/integration/sage/database.py` ;
- `app/integration/sage/writer.py` ;
- des données SQLite ou Sage.

---

## 5. Tests

Fichier `tests/test_sfec_niu.py` :

- `business` sans NIU → `individual`, payload valide ;
- `government` sans NIU → `individual`, payload valide ;
- `foreign` sans NIU → `individual`, payload valide ;
- log `Client sans NIU` avec code tiers ;
- NIU invalide → toujours refusé ;
- NIU valide → type conservé ;
- NIU entreprise → uniquement `taxpayer_niu` ;
- client comptoir → `individual`, aucune donnée contact modifiée.

Commande :

```bash
cd connecteur-python
pytest -q tests/test_sfec_niu.py
```

Résultat : **16 tests passés**.

---

## 6. Limite connue : factures déjà `A_SURVEILLER`

Cette évolution ne réarme pas les factures déjà marquées `A_SURVEILLER`.
L'auto-certification continue de les ignorer :

```python
if sfec_statut in ("EN_COURS", "ERREUR", "A_SURVEILLER"):
    continue
```

Les factures concernées devront être relancées après déploiement. La relance
manuelle existante (`/pending` → « Relancer ») remet uniquement le statut
`A_SURVEILLER` à `NULL` pour la pièce concernée.

Aucune reprise groupée n'est incluse dans ce lot, conformément à la demande
de ne pas toucher au miroir SQLite ni d'effectuer de modification massive.

---

## 7. Annexe — logo de la page de connexion

Le logo du login est remplacé par celui de la société **BAYO** :

- ajout de `app/web/static/bayo.png` ;
- `app/web/templates/auth/login.html` utilise `/static/bayo.png` pour le
  bandeau et le logo principal ;
- pied de page mis à jour : `T-CONNECTOR SFEC © 2026 — BAYO`.

Aucun autre écran n'est rebrandé dans ce lot.
