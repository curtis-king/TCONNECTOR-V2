# Proposition 05 — Avoirs, références et pièces négatives

> **Priorité** : 🟠 P1 — ~41 000 warnings, aucun avoir certifié.
> **Fichiers** : `app/sync/engine.py`, `app/integration/sfec/endpoints.py`, `app/integration/sage/writer.py`, `app/integration/sage/database.py`, `app/sync/bidirectional.py`, `app/storage/db.py`, `app/web/routes/billing.py`, `app/web/auth/security.py`
> **Tests** : `tests/test_sfec_avoirs.py` (20 tests)
> **Réf. rapport** : `00-rapport-analyse-logs.md` §3.5

---

## 0. État d'implémentation — 22/10/2026

Correctif appliqué dans le cadre de la proposition 05.

| Point | Implémentation réelle |
|---|---|
| Résolution | `resolve_invoice_number()` dans `endpoints.py:84` : lien manuel, exact, compact, token, sous-chaîne (du plus long au plus court). |
| Avoirs | `_prepare_credit_note()` dans `engine.py:184`, appelé par l'auto-certification, la file de reprise et `certify_single()`. |
| Parent non certifié | Aucun marquage : l'avoir reste hors file et est réévalué au cycle suivant. |
| Parent absent / référence invalide | `A_SURVEILLER` avec motif, jamais `ERREUR` ni rejeu SFEC. |
| Factures à 0 | `_has_zero_amount()` (`engine.py:111`) : `A_SURVEILLER`, mais un champ `montant_ttc` **absent** n'est pas traité comme zéro. |
| Factures négatives | Converties en `creditNote` à montants positifs **si un parent est résolu**. Les 5 585 pièces négatives sans parent restent hors périmètre automatique, sans être noyées dans `A_SURVEILLER`, en attente de la décision client (§6.4 du rapport). |
| Champs SFEC | `reference_invoice_id`, `external_credit_note_number`, `original_sfec_invoice_number` obligatoires dans `validate_sfec_payload()` (`endpoints.py:627`). |
| Numéro du parent | `sfec-certified.json` (`invoice_number`), puis `GET /api/v1/invoices`, puis numéro Sage. Pas de colonne SQLite supplémentaire : le fichier est déjà la source de vérité des certifications. |
| Miroir local | `writer.py:358` et `bidirectional.py:150` écrivent `type_doc`, `sage_domaine`, `sage_type`, `sage_piece`, `montant_tva` ; les montants négatifs sont typés `avoir`, y compris lors d'un pull sur une ligne existante. |
| Lien manuel | Table `sfec_invoice_links(numero, parent_numero)` (`storage/db.py:654`) + `POST /api/invoices/<path:numero>/link-parent` (`billing.py:373`). Le lien remet le statut `A_SURVEILLER` à vide (`reset_sfec_monitor()`, `sage/database.py:1749`) pour autoriser un nouveau cycle, sans toucher à `ERREUR`/`CERTIFIE`. |

Les extraits ci-dessous restent la proposition initiale ; les noms exacts sont ceux du tableau ci-dessus.

---

## 1. Symptômes

```
[t-connector.db] WARNING: Facture marquee ERREUR certification SFEC:
  0-7-25VBA2818 - Facture d'origine '25VBA2818' non certifiee           (60x)
  0-7-... - Avoir sans reference facture d'origine                      (2 200x)
  0-7-... - Facture d'origine 'N°BC78799' non certifiee                 (40x)
  0-7-... - Facture d'origine 'KANE' non certifiee                      (40x)
  ...
```

**41 178** lignes sur la fenêtre de 1 h 30. Références observées : `25VBA2818`, `N°BC78799`, `KANE`, `UNI PRIX`, `SNC`, `SIBCL25080`, `N°PO2500422`, `ZEN`… → du **texte libre**, pas un numéro de pièce.

En base locale :

```text
factures montant_ttc < 0            -> 5 585
factures montant_ttc = 0            ->    36
type_doc = 'avoir'                  ->     0
sage_type = 7                       ->     3   (le reste est force a 6)
```

## 2. Causes racines

### 2.1 Référence libre non résolue

`engine.py:573-597` exige `inv["reference_invoice_id"]` **ou** `inv["reference"]` soit **exactement** le `numero` d'une facture du cache :

```python
ref_invoice = by_numero.get(ref)
```

Or `DO_Ref` contient une saisie libre (`N°BC78799`, nom du client, bon de commande). Aucun avoir ne peut donc être rattaché.

### 2.2 Le parent non certifié est traité comme une erreur définitive

Si le parent existe mais n'est pas encore certifié, l'avoir est marqué `ERREUR`. Combiné à `reset_stuck_en_cours` (proposition 02), il est rejoué à l'infini. Il faudrait **attendre** et réévaluer au cycle suivant.

### 2.3 Factures négatives typées « vente »

Une facture d'avoir saisie en Sage comme `DO_Type = 6` (facture) avec des montants négatifs est envoyée en `salesInvoice` avec `total_amount < 0` :

```
total_amount doit etre > 0 (actuel: -796009.0); items[0].total_amount doit etre > 0
```

### 2.4 Factures à 0

36 factures `montant_ttc = 0` → `total_amount doit etre > 0` (180 + 62 occurrences). Rien à certifier.

### 2.5 Miroir local : `type_doc` et `sage_type` faux

`writer.py:402-423` code `sage_type = 6` en dur et n'écrit jamais `type_doc` → aucun avoir dans le miroir, donc l'UI ne peut pas les lister.

## 3. Correctif

### 3.1 Résolution d'une référence libre

Ajouter dans `app/integration/sfec/endpoints.py` (ou un module `app/integration/sfec/references.py`) :

```python
import re

# 25VFA3122, 9FAZ00124, VBA2818, FA00001...
_REF_TOKEN = re.compile(r"\b\d{0,2}[A-Z]{2,4}\d{3,}\b")


def resolve_invoice_number(raw, by_numero):
    """Retrouve un numero de piece Sage dans une reference libre.

    Ordre : exact -> sans espaces/casse -> token ressemblant a un numero
    -> sous-chaine. Retourne None si rien ne correspond (l'avoir part en
    A_SURVEILLER, il n'est pas certifie avec une reference inventee).
    """
    if not raw:
        return None
    text = str(raw).strip()
    if text in by_numero:
        return text

    compact = text.upper().replace(" ", "")
    for numero in by_numero:
        if numero.upper() == compact:
            return numero

    for token in _REF_TOKEN.findall(text.upper()):
        if token in by_numero:
            return token

    for numero in by_numero:
        if len(numero) >= 6 and numero.upper() in text.upper():
            return numero

    return None
```

### 3.2 Bloc avoir — `engine.py:573-597`

**Bloc à remplacer**

```python
        if type_doc == "avoir":
            ref = inv.get("reference_invoice_id") or inv.get("reference", "")
            if not ref:
                try:
                    mark_certification_failed(inv["id"], "Avoir sans reference facture d'origine")
                except Exception:
                    pass
                continue
            ref_invoice = by_numero.get(ref)
            if not ref_invoice or ref_invoice.get("sfec_statut") not in ("CERTIFIE", "DEJA_CERTIFIE"):
                try:
                    mark_certification_failed(inv["id"], "Facture d'origine '{}' non certifiee".format(ref))
                except Exception:
                    pass
                continue
            _ids_ref = avoir_ids_par_ref.get(ref, [])
            deja_avoir = any(_i != inv.get("id") for _i in _ids_ref)
            if deja_avoir:
                try:
                    mark_certification_failed(inv["id"], "Un avoir deja certifie existe pour '{}'".format(ref))
                except Exception:
                    pass
                continue
```

**Bloc corrigé**

```python
        if type_doc == "avoir":
            raw_ref = inv.get("reference_invoice_id") or inv.get("reference", "")
            ref = resolve_invoice_number(raw_ref, by_numero)

            if not ref:
                try:
                    mark_to_monitor(
                        inv["id"],
                        "Avoir: reference d'origine introuvable ({!r})".format(raw_ref),
                    )
                except Exception:
                    pass
                continue

            ref_invoice = by_numero.get(ref)
            if not ref_invoice or ref_invoice.get("statut_code") != 2:
                try:
                    mark_to_monitor(inv["id"], "Avoir: facture d'origine '{}' absente".format(ref))
                except Exception:
                    pass
                continue

            if ref_invoice.get("sfec_statut") not in ("CERTIFIE", "DEJA_CERTIFIE"):
                # Le parent n'est pas encore certifie : on ATTEND. Ne pas
                # marquer ERREUR (sinon rejeu infini) ni A_SURVEILLER (le
                # cas se resout tout seul au prochain cycle).
                logger.debug("Avoir %s en attente de certification de %s", numero, ref)
                continue

            _ids_ref = avoir_ids_par_ref.get(ref, [])
            deja_avoir = any(_i != inv.get("id") for _i in _ids_ref)
            if deja_avoir:
                try:
                    mark_to_monitor(inv["id"], "Un avoir certifie existe deja pour '{}'".format(ref))
                except Exception:
                    pass
                continue

            # Necessaire pour db_invoice_to_sfec() -> reference_invoice_id
            inv["reference_invoice_id"] = ref
            # Champs exiges par l'API SFEC pour un creditNote (OpenAPI) :
            #  - external_credit_note_number  = numero de l'avoir dans l'ERP
            #  - original_sfec_invoice_number = numero de la facture d'origine connu de SFEC
            inv["external_credit_note_number"] = numero
            inv["original_sfec_invoice_number"] = (
                ref_invoice.get("sfec_invoice_number") or ""
            )
            if not inv["original_sfec_invoice_number"]:
                try:
                    parent = SfecClient().verify_by_invoice_number(ref)
                    inv["original_sfec_invoice_number"] = (
                        (parent or {}).get("invoice_number") or ""
                    )
                except Exception:
                    inv["original_sfec_invoice_number"] = ""
            # Repli : le numero ERP du parent est le plus souvent celui connu
            # par SFEC (champ invoice_number). Ne jamais utiliser la signature.
            inv["original_sfec_invoice_number"] = (
                inv["original_sfec_invoice_number"] or ref
            )
```

Appliquer les mêmes règles dans `certify_single()` (`engine.py:700-730`). Voir §3.7 pour la nature exacte de `original_sfec_invoice_number`.

### 3.3 Factures à 0

Dans la sélection (`engine.py:557`), après le contrôle `statut_code != 2` :

```python
def _has_zero_amount(invoice):
    """Ne confond pas un champ absent (tests, objet partiel) avec 0."""
    value = invoice.get("montant_ttc")
    if value is None or value == "":
        return False
    return abs(_as_float(value, 0)) < 0.01
```

Puis, dans la sélection :

```python
        if _has_zero_amount(inv):
            try:
                mark_to_monitor(inv["id"], "Montant nul: non certifiable")
            except Exception:
                pass
            continue
```

> Le contrôle sur `statut_code == 2` reste prioritaire : une facture à 0 non comptabilisée est ignorée avant ce bloc.

### 3.4 Factures négatives → `creditNote`

Deux options, à trancher avec le client.

**Option A (recommandée) — traiter en avoir SFEC, montants absolus.**

Dans `db_invoice_to_sfec()` (`endpoints.py:270`) :

```python
    invoice_type = _map_invoice_type(invoice.get("type_doc", "vente"))
    if invoice_type == "salesInvoice" and _safe_float(invoice.get("montant_ttc"), 0) < 0:
        # Facture d'avoir saisie en DO_Type=6 avec des montants negatifs :
        # SFEC attend un creditNote a montants positifs.
        invoice_type = "creditNote"
```

Puis, juste avant de construire `sfec_req`, normaliser :

```python
    amount_due = round(_safe_float(invoice.get("montant_restant"), total_amount), 2)
    if invoice_type == "creditNote":
        # SFEC attend un creditNote a montants POSITIFS : on normalise
        # en valeur absolue, quel que soit le signe stocke dans Sage.
        subtotal = round(abs(subtotal), 2)
        total_tax_t = round(abs(total_tax_t), 2)
        total_tax_r = round(abs(total_tax_r), 2)
        total_exempt = round(abs(total_exempt), 2)
        total_tax = round(abs(total_tax), 2)
        additional_cent_tax = round(abs(additional_cent_tax), 2)
        additional_taxes = [
            dict(t, tax_amount=round(abs(_safe_float(t.get("tax_amount"), 0)), 2))
            for t in additional_taxes
        ]
        total_amount = round(abs(total_amount), 2)
        for item in items:
            for key in ("unit_price", "subtotal", "discount_amount",
                        "net_amount", "tax_amount", "total_amount"):
                item[key] = round(abs(item[key]), 2)
        # amount_due est calcule depuis montant_restant : le normaliser aussi.
        amount_due = round(abs(amount_due), 2)
```

Et dans le dictionnaire `sfec_req` (`endpoints.py:292`), utiliser la variable normalisée :

```python
        "amount_due": amount_due,
```

⚠️ **À valider en sandbox** : vérifier que `POST /api/v1/invoices` avec `invoice_type = creditNote` et `reference_invoice_id` attend bien des montants **positifs**. Tester une facture réelle (`25VFR0137`) en `use_sandbox: true` avant de généraliser.

**Option B (conservatrice) — exclure du périmètre automatique.**

```python
        if _safe_float(inv.get("montant_ttc"), 0) < 0 and type_doc != "avoir":
            mark_to_monitor(inv["id"], "Facture negative sans type avoir: a traiter manuellement")
            continue
```

**Décision retenue : hybride A/B.**

- Une facture négative dont la référence est résolue vers un parent **certifié** part en `creditNote` (Option A).
- Une facture négative sans parent résolu est **ignorée silencieusement** (log `DEBUG`), sans être marquée : les 5 585 pièces concernées ne doivent pas saturer la liste `A_SURVEILLER` avant l'arbitrage client. Un lien manuel suffit à les faire entrer dans le périmètre.
- Les vrais avoirs (`type_doc = 'avoir'`, `DO_Type = 7`) restent marqués `A_SURVEILLER` quand leur référence est invalide.
- Dans tous les cas, `db_invoice_to_sfec()` convertit une pièce à montants négatifs en `creditNote` positif : aucune ne peut partir en `salesInvoice` avec `total_amount < 0`.

### 3.5 Correction du miroir local

`writer.py:402-423` :

- remplacer `VALUES (..., 1, 0, 6, ?)` par `VALUES (..., 1, ?, ?, ?)` avec `sage_domaine`, `sage_type` (issu de `do_type_raw`) et `sage_piece` ;
- écrire `type_doc` (`avoir` si `do_type_raw == 7` ou montant négatif) ;
- écrire `montant_tva` (voir `07-proposition-miroir-local.md`).

Le cache `fetch_sales_invoices` expose déjà `type_doc` et `do_type_raw`.

### 3.6 Liaison manuelle (filet de sécurité)

Route `POST /api/invoices/<path:numero>/link-parent` (`billing.py:373`) permettant de saisir le numéro de la facture d'origine d'un avoir. Corps JSON : `{"parent_numero": "..."}`. La valeur est stockée dans `sfec_invoice_links(numero, parent_numero)` (`storage/db.py:654`) et consultée en priorité par le moteur avant la résolution textuelle.

La route vérifie que le parent existe dans le miroir ou le cache Sage et qu'il n'est pas lui-même négatif. Elle exige la permission `factures.creer` (`security.py:190`) et journalise l'action dans l'audit. Elle remet le statut `A_SURVEILLER` de l'avoir à vide (Sage et miroir) pour qu'il soit réévalué au cycle suivant.

Cela couvre les références définitivement non exploitables (`KANE`, `ZEN`, `SNC`).

### 3.7 Champs SFEC obligatoires pour un avoir

L'OpenAPI (`CertifyInvoiceRequest`) impose **deux champs supplémentaires** pour `invoice_type = creditNote` :

| Champ | Valeur | Source |
|---|---|---|
| `external_credit_note_number` | Numéro de l'avoir dans l'ERP | `inv["numero"]` (Sage) |
| `original_sfec_invoice_number` | Numéro de la facture d'origine **tel que connu par SFEC** | `GET /api/v1/invoices` → `invoice_number` du parent ; à défaut, le numéro Sage du parent |

L'OpenAPI illustre ce champ par `CG-2026-0000000021`, mais le `GET` expose `invoice_number` = « numéro de facture transmis par l'ERP » (pas un numéro SFEC distinct). Deux cas :

- le client possède un **numéro SFEC imprimé** sur la facture certifiée (`CG-…`) → l'utiliser ;
- sinon → utiliser l'`invoice_number` du parent, qui peut être **identique** à `reference_invoice_id`. C'est acceptable : les deux champs sont envoyés ensemble.

⚠️ `original_sfec_invoice_number` **n'est jamais** :
- `certification_number` / `sfec_num_certif` (la signature de 64 caractères) ;
- `DO_Ref` (texte libre) ;
- un numéro inventé.

Dans `db_invoice_to_sfec()` (`endpoints.py:311-314`), recopier les champs dans le payload :

**Bloc à remplacer**

```python
    if invoice_type == "creditNote":
        ref_invoice_id = _safe_str(invoice.get("reference_invoice_id"))
        if ref_invoice_id:
            sfec_req["reference_invoice_id"] = ref_invoice_id
```

**Bloc corrigé**

```python
    if invoice_type == "creditNote":
        ref_invoice_id = _safe_str(invoice.get("reference_invoice_id"))
        if ref_invoice_id:
            sfec_req["reference_invoice_id"] = ref_invoice_id
        for key in ("external_credit_note_number", "original_sfec_invoice_number"):
            value = _safe_str(invoice.get(key))
            if value:
                sfec_req[key] = value
```

Et dans `validate_sfec_payload()` (`endpoints.py:367-369`), exiger les deux champs :

```python
    if payload.get("invoice_type") == "creditNote":
        if not payload.get("reference_invoice_id"):
            errors.append("reference_invoice_id requis pour un avoir (creditNote)")
        if not payload.get("external_credit_note_number"):
            errors.append("external_credit_note_number requis pour un avoir")
        if not payload.get("original_sfec_invoice_number"):
            errors.append("original_sfec_invoice_number requis pour un avoir")
```

Il faut donc **stocker l'`invoice_number` du parent** au moment de sa certification. Le fichier `sfec-certified.json` contient déjà la clé `invoice_number` pour chaque entrée (`endpoints.py:784`) : c'est la source de vérité retenue, complétée en mémoire par `invoice["sfec_invoice_number"]` après `certify_invoice()` / `_handle_already_certified()`. Aucune colonne SQLite supplémentaire n'est nécessaire.

`_parent_sfec_number()` (`engine.py:154`) tente dans l'ordre :

1. `invoice["sfec_invoice_number"]` en mémoire ;
2. `get_certified_by_sage_id(parent["id"])` dans `sfec-certified.json` ;
3. `GET /api/v1/invoices` par numéro de vendeur (`verify_by_invoice_number`) ;
4. repli sur le numéro Sage `ref`.

L'avoir n'est donc pas bloqué si SFEC ne renvoie pas de numéro distinct. Ne jamais utiliser `certification_number` / `sfec_num_certif` (signature de 64 caractères).

On envoie les **trois** champs (`reference_invoice_id`, `external_credit_note_number`, `original_sfec_invoice_number`) : la doc de certification mentionne encore `reference_invoice_id`, l'OpenAPI exige les deux autres. Les envoyer ensemble couvre les deux versions de l'API.

> **À valider en sandbox** : certifier un avoir de test et vérifier que `original_sfec_invoice_number = référence du parent` est accepté. Si SFEC renvoie 400 sur ce champ, c'est que le numéro `CG-…` est réellement obligatoire : dans ce cas, le faire saisir par le client (page « À surveiller ») et marquer l'avoir `A_SURVEILLER` en attendant — sans rejeu automatique.

## 4. Tests

`tests/test_sfec_avoirs.py` :

```python
from app.integration.sfec.endpoints import resolve_invoice_number

BY = {"25VFA3122": {}, "9FAZ00124": {}}

def test_reference_exacte():
    assert resolve_invoice_number("25VFA3122", BY) == "25VFA3122"

def test_reference_avec_prefixe_libre():
    assert resolve_invoice_number("N° BC 25VFA3122 du 27/07", BY) == "25VFA3122"

def test_reference_invalide():
    assert resolve_invoice_number("KANE", BY) is None

def test_reference_ambigue():
    assert resolve_invoice_number("ZEN", BY) is None
```

Tests moteur (monkeypatch `certify_invoice`, `mark_to_monitor`) :

1. avoir sans référence → `A_SURVEILLER`, **pas** `ERREUR`, `certify_invoice` non appelé ;
2. avoir dont le parent n'est pas certifié → **aucun marquage**, `certify_invoice` non appelé ;
3. avoir dont le parent est certifié → `certify_invoice` appelé avec `reference_invoice_id` renseigné ;
4. deuxième avoir pour le même parent → `A_SURVEILLER` ;
5. facture à 0 → `A_SURVEILLER` ;
6. facture négative (Option A) → payload `creditNote` avec `total_amount > 0`, `reference_invoice_id`, `external_credit_note_number` et `original_sfec_invoice_number` ;
7. avoir dont le parent est certifié mais sans `sfec_invoice_number` → `GET` de secours, puis repli sur le numéro Sage ; le payload contient toujours les trois champs ;
8. facture négative sans référence → **aucun marquage** (hors périmètre, décision client en attente) ;
9. facture négative avec parent certifié → `certify_invoice` appelé en `creditNote` ;
10. miroir : `sync_sage_invoice_to_sqlite()` écrit `type_doc='avoir'`, `sage_type=7`, `sage_domaine`, `sage_piece`, `montant_tva` ;
11. table `sfec_invoice_links` : `set/get/fetch` fonctionnent ;
12. `validate_sfec_payload()` refuse un `creditNote` sans les deux champs OpenAPI ;
13. lien manuel vers un parent hors cache : `GET` SFEC de vérification, puis `original_sfec_invoice_number` renseigné ;
14. pull sur une ligne existante : `_update_from_sage()` corrige `type_doc`, `sage_type` et `montant_tva` ;
15. route `link-parent` : lien enregistré et réponse 200 (le reset Sage est mocké).

Résultat local : **20 tests passent** (`pytest tests/test_sfec_avoirs.py -q`).

## 5. Critères d'acceptation

- [x] 0 warning `Avoir sans reference` / `Facture d'origine … non certifiee` en régime nominal.
- [x] Les avoirs dont le parent est certifié sont certifiés au cycle suivant.
- [x] Les avoirs non résolubles apparaissent en `A_SURVEILLER` avec la référence brute.
- [x] Aucune facture négative n'est envoyée en `salesInvoice`.
- [x] Tout `creditNote` envoyé contient `external_credit_note_number` **et** `original_sfec_invoice_number` (numéro SFEC du parent, pas la signature).
- [x] Les 36 factures à 0 ne sont plus tentées.
- [x] Les 5 585 factures négatives sans parent ne polluent pas `A_SURVEILLER` et restent hors périmètre jusqu'à décision client / lien manuel.
- [ ] Validation sandbox du `creditNote` réel (`25VFR0137`) et du numéro `original_sfec_invoice_number` avant généralisation.

## 6. Risques / rollback

| Risque | Parade |
|---|---|
| SFEC refuse les montants positifs pour un `creditNote` | Test sandbox préalable ; Option B en repli |
| Résolution vers la mauvaise facture (sous-chaîne) | Ordre exact → compact → token → sous-chaîne ; journaliser chaque résolution |
| Un avoir référence un avoir | `ref_invoice.type_doc` doit être `vente` |
| Le parent n'arrive jamais | Reste visible dans « À surveiller » avec un âge > 7 jours |

**Rollback** : revert `engine.py` + `endpoints.py` ; les avoirs repassent en `ERREUR` au cycle suivant (comportement actuel).
