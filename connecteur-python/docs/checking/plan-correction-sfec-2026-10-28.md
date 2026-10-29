# Plan de correction — Intégration SFEC TConnector

**Date du plan :** 28/10/2026  
**Référence :** `docs/checking/rapport-api-sfec-sandbox-2026-10-28.md`  
**Périmètre :** `app/integration/sfec/`, `app/sync/engine.py`, `app/storage/db.py`, `app/web/routes/billing.py`, `app/web/templates/billing/`, `tests/`  
**Environnement de validation :** sandbox SFEC uniquement  
**Statut :** plan de travail — aucun code applicatif modifié à ce stade

## 1. Objectif

Remédier à tous les écarts confirmés entre le comportement réel de l’API SFEC et le connecteur, en priorisant :

1. la fiabilité de la récupération des certifications existantes ;
2. la validation locale des données avant envoi ;
3. le support complet des avoirs, y compris `creditNoteOnAccount` et les avoirs partiels ;
4. la robustesse réseau et la traçabilité des rejets ;
5. la non-régression via des tests unitaires, de contrat et sandbox.

### Principes directeurs

- **SFEC n’est pas le validateur métier.** Le connecteur doit refuser localement ce que l’API accepte à tort.
- **Aucune donnée inventée en production.** Pas de NIU, téléphone ou email factice par défaut.
- **Aucun fallback silencieux.** Un mode de paiement inconnu, un taux invalide ou un total incohérent doit produire un motif exploitable, pas une certification approximative.
- **Les avoirs partiels restent autorisés.** Un avoir sur-soldé est un rejet définitif, pas une erreur transitoire.
- **Idempotence.** Un `409` doit permettre de récupérer une certification existante sans jamais marquer une facture `DEJA_CERTIFIE` sans preuve.
- **Tests avant livraison.** Chaque correction doit avoir un test qui échoue avant et passe après.

## 2. Matrice de traçabilité

| ID | Constat du rapport | Impact | Priorité | Lot |
|---|---|---|---|---|
| SFEC-01 | `verify_by_invoice_number()` ne lit que la page 1 | Factures existantes non retrouvées, reprises `409` impossibles | P0 | 1 |
| SFEC-02 | `_build_sfec_lookup()` utilise `page_size=500` et `total` | Lookup incomplet ou boucle incorrecte | P0 | 1 |
| SFEC-03 | `verify_certification()` lit `certification_number`/`short_signature` | Retourne `False` sur une facture certifiée | P1 | 1 |
| SFEC-04 | Dates envoyées en `YYYY-MM-DD` | `invoice_due_date` et `payment_date` stockés à `null` | P0 | 2 |
| SFEC-05 | `payment_method` brut, mapping POS incomplet | Mode de paiement déclaré à tort | P1 | 2 |
| SFEC-06 | `unit_price=0`, `quantity=0`, `tax_rate=20` acceptés | Factures fiscalement incohérentes certifiées | P0 | 2 |
| SFEC-07 | Totaux non réconciliés par l’API | `tva`, `total_ttc`, `amount_due` incohérents | P0 | 2 |
| SFEC-08 | Longueurs non validées | `500` SFEC au lieu d’un rejet local | P1 | 2 |
| SFEC-09 | NIU/téléphone/email factices par défaut | Données destinataire erronées | P1 | 2 |
| SFEC-10 | `creditNoteOnAccount` non supporté | Avoirs sans parent envoyés en `creditNote` invalide | P1 | 3 |
| SFEC-11 | Avoir sur-soldé renvoie `400`, pas `409` | Rejet mal classé, risque de rejeu | P1 | 3 |
| SFEC-12 | `creditNoteOnAccount` : doublon de référence → `409` | Confusion avec un doublon d’`invoice_id` | P1 | 3 |
| SFEC-13 | `429` non traité | Rejeu immédiat ou abandon | P1 | 4 |
| SFEC-14 | Lookup complet coûteux (27 pages) | Démarrage lent, charge API | P2 | 4 |
| SFEC-15 | Débours non supportés par le connecteur | Net dû incomplet si le client facture des débours | P2 | 5 |
| SFEC-16 | Tri de liste instable | Export exhaustif non fiable | P2 | 5 |
| SFEC-17 | Tests `test_sage_odbc_409.py` encodent l’ancien comportement | Faux verts / non-régression impossible | P0 | 1 |
| SFEC-18 | `config.py` demande `page_size=100` | L’API ramène à 20, page incomplète | P2 | 4 |

## 3. Lot 0 — Préparation et filet de sécurité

### 3.1 Prérequis

- [ ] Sauvegarder `config.json` avant toute modification.
- [ ] Utiliser une clé sandbox via `SFEC_SANDBOX_API_KEY` ou `sfec.sandbox_api_key`, jamais dans un fichier versionné.
- [ ] Installer `pytest` dans le venv du projet si absent :
  - Linux : `.venv/bin/python -m pip install -r requirements-dev.txt` ou `pytest>=9`.
  - Windows : `"%VENV_PY%" -m pip install ...`.
- [ ] Vérifier que les tests sandbox restent désactivés par défaut (`TCONNECTOR_TEST_SFEC_SANDBOX=1` pour les activer).
- [ ] Créer une fixture JSON de réponses SFEC anonymisées pour les tests de contrat.

### 3.2 Tests à écrire avant les correctifs

Créer ou compléter :

- `tests/test_sfec_client.py` : récupération par numéro, pagination, champs de certification, `429`, `404`, `5xx`.
- `tests/test_sfec_payload_validation.py` : dates, paiement, taux, quantités, prix, totaux, longueurs, destinataires.
- `tests/test_sfec_credit_notes.py` : avoir classique, partiel, sur-soldé, `creditNoteOnAccount`, doublon de référence.
- `tests/test_sage_odbc_409.py` : adapter les mocks de `list_invoices` vers `get_invoice`.
- `tests/perf/mocks/sfec.py` : ajouter `get_invoice` et aligner `page_size` sur 20.

## 4. Lot 1 — Récupération et vérification des certifications

### 4.1 `SfecClient.get_invoice()` : encoder l’identifiant

**Fichier :** `app/integration/sfec/client.py`

- Encoder le numéro avec `urllib.parse.quote(value, safe="")` pour gérer `/`, `#`, espaces et caractères non ASCII.
- Refuser un identifiant vide avant l’appel.
- Conserver le comportement `404` → exception avec `status_code=404`.

```python
from urllib.parse import quote

def get_invoice(self, sfec_id):
    value = str(sfec_id or "").strip()
    if not value:
        raise ValueError("sfec_id requis")
    return self._request("GET", "/api/v1/invoices/{}".format(quote(value, safe="")))
```

**Test :** `FCDFG013` → URL `/api/v1/invoices/FCDFG013` ; `A/B` → `A%2FB`.

### 4.2 `verify_by_invoice_number()` : appel direct au détail

**Fichier :** `app/integration/sfec/client.py`

Remplacer `_verify_by_invoice_number_once()` et sa boucle de pagination par :

```python
def verify_by_invoice_number(self, invoice_number):
    numero = str(invoice_number or "").strip()
    if not numero:
        return None
    for attempt in range(3):
        try:
            return self.get_invoice(numero)
        except Exception as exc:
            status = int(getattr(exc, "status_code", 0) or 0)
            if status == 404:
                return None
            if not _is_retryable_verify_error(exc) or attempt >= 2:
                return None
            time.sleep(_retry_delay(exc, attempt))
    return None
```

Points d’implémentation :

- Supprimer `_verify_by_invoice_number_once()` et toute référence à `page_size=500`.
- `404` : retour immédiat `None`, sans retry.
- `400`/`422` : retour immédiat `None`, sans retry.
- `429`, `5xx`, timeout, erreur réseau : 3 tentatives avec backoff (`2 s`, `5 s`, ou `Retry-After`).
- Ne jamais transformer un `404` en “facture certifiée”.
- Le détail accepte le numéro de facture ; c’est le chemin nominal.

**Fallback optionnel :** si un ancien enregistrement SFEC n’a pas `invoice_number == numero` mais seulement `seller_invoice_number`, faire un second passage paginé **uniquement après un `404`** et avec `pageSize=20`, `totalPages`, arrêt sur liste vide. Ce fallback doit être explicitement testé et journalisé.

### 4.3 `_build_sfec_lookup()` : pagination correcte

**Fichier :** `app/sync/engine.py`

- Utiliser `page_size=20`.
- Lire `totalPages` ; ne plus lire `total`/`total_count`.
- Arrêter sur liste vide **ou** `page >= totalPages`.
- Ajouter une garde `max_pages` configurable (`sfec.lookup_max_pages`, défaut 500) pour éviter une boucle infinie.
- Indexer à la fois `seller_invoice_number` et `invoice_number`.
- Invalider le cache sur `409` comme aujourd’hui.

```python
page = 1
while page <= max_pages:
    result = client.list_invoices(page=page, page_size=20)
    invoices = result.get("invoices") or []
    if not invoices:
        break
    ...
    total_pages = int(result.get("totalPages") or 0)
    if total_pages and page >= total_pages:
        break
    page += 1
```

**Performance :** si le lookup complet reste nécessaire, conserver le cache de 5 minutes. Sinon, prévoir un mode « à la demande » par `get_invoice` pour les factures à certifier, avec un cache local. Ce point est détaillé au lot 4.

### 4.4 `verify_certification()` : lire les bons champs

**Fichier :** `app/integration/sfec/client.py`

- Chercher `certification_short_signature` ou `certification_signature`.
- Éventuellement accepter `certification_status == "certified"` en complément.
- Ne jamais chercher `certification_number`/`short_signature` dans le `GET`.
- Si la méthode n’est pas utilisée, choisir explicitement entre la corriger et la supprimer. **Recommandation :** la corriger et la couvrir par un test, car elle fait partie de l’API publique du client.

```python
def verify_certification(self, identifier):
    try:
        result = self.get_invoice(identifier)
        return bool(
            result.get("certification_short_signature")
            or result.get("certification_signature")
            or result.get("certification_status") == "certified"
        )
    except Exception:
        return False
```

### 4.5 Tests du lot 1

- `test_verify_by_invoice_number_utilise_get_invoice` : vérifier qu’aucun `list_invoices` n’est appelé.
- `test_verify_by_invoice_number_trouve_facture_page_2` : mock `get_invoice` direct ; le test historique de pagination devient un test de non-régression du lookup.
- `test_verify_by_invoice_number_404_sans_retry`.
- `test_verify_by_invoice_number_429_respecte_retry_after`.
- `test_verify_by_invoice_number_5xx_retente`.
- `test_verify_certification_lit_certification_short_signature`.
- `test_get_invoice_encode_identifiant`.
- `test_build_sfec_lookup_utilise_total_pages` : deux pages, cible en page 2.

**Critère d’acceptation :** une facture présente en page 2 est retrouvée sans scan complet ; `verify_certification('FCDFG013')` retourne `True` sur la fixture réelle.

## 5. Lot 2 — Payload : dates, paiement, lignes, totaux, destinataires

### 5.1 Dates en RFC 3339

**Fichier :** `app/integration/sfec/endpoints.py`

- Remplacer `_normalize_date()` par `_normalize_datetime()`.
- Accepter :
  - `datetime` ;
  - `date` ;
  - `YYYY-MM-DD` ;
  - `YYYY-MM-DD HH:MM:SS` ;
  - ISO 8601 avec fuseau (`Z`, `+01:00`) ;
  - `DD/MM/YYYY` et `DD-MM-YYYY` (formats Sage).
- Sortie : `YYYY-MM-DDTHH:MM:SSZ` pour une date seule (`00:00:00Z`), conservation du fuseau puis normalisation UTC pour un timestamp complet.
- `1900-01-01`, vide et `None` → `None`.
- Utiliser pour `invoice_due_date` et `payment_date`.
- Ne pas envoyer `invoice_date` : SFEC le génère à la certification.

```python
def _normalize_datetime(value):
    # date seule -> "2026-12-31T00:00:00Z"
    # timestamp complet -> normalisé en UTC avec suffixe Z
    ...
```

**Tests :** date seule, timestamp avec `Z`, timestamp avec `+01:00`, format Sage, date invalide, `1900-01-01`.

### 5.2 Mapping des moyens de paiement

**Fichiers :** `app/integration/sfec/endpoints.py`, `app/domain/pos.py`, éventuellement `app/integration/sfec/mappings.py`

Créer un mapping unique et testé :

| Source | SFEC |
|---|---|
| `cash`, `especes`, `espèces` | `cash` |
| `check`, `cheque`, `chèque` | `cheque` |
| `bank_transfer`, `virement`, `transfer` | `bank_transfer` |
| `card`, `carte` | `card` |
| `mobile_money`, `mobile`, `momo` | `mobile_money` |
| `credit`, `credit_note` | `credit` |

Règles :

- Corriger `_MODE_PAIEMENT_TO_SFEC` dans `pos.py` : `cheque` → `cheque`, pas `check`.
- Utiliser le même mapping dans `db_invoice_to_sfec()` et `sqlite_invoice_to_sfec()`.
- Pour les codes Sage numériques (`DO_ModeReglement`), ajouter une table configurable `sfec.payment_method_map` (ex. `"1": "cash"`, `"3": "bank_transfer"`), à confirmer sur la base Sage du client.
- Un code inconnu ne doit pas être déclaré silencieusement `bank_transfer`. Deux options :
  - **recommandé** : rejet local `422` avec motif `payment_method inconnu: <valeur>` ;
  - **tolérant** : fallback configurable `bank_transfer` avec avertissement explicite.
- `mixte` n’a pas d’équivalent SFEC : décision métier à valider (voir §11). Par défaut, ne pas inventer ; le rejeter ou le mapper via configuration.

**Tests :** chaque valeur texte, codes numériques configurés, valeur inconnue, `mixte`, `cheque` POS.

### 5.3 Validation des lignes

**Fichier :** `app/integration/sfec/endpoints.py`

Dans `db_invoice_to_sfec()` :

- Ne plus faire `quantity = max(quantity, 1)` ni `unit_price = max(unit_price, 0)` de façon silencieuse.
- Pour un avoir, conserver la valeur absolue, mais vérifier qu’elle est strictement positive.
- Règles minimales :
  - `quantity > 0` ;
  - `unit_price > 0` ;
  - `tax_rate` ∈ `{"0", "5", "18"}` ;
  - `subtotal >= 0` ;
  - `0 <= discount_amount <= subtotal` ;
  - `net_amount` (TTC) et `total_amount` cohérents avec `subtotal + tax_amount` ;
  - `amount_after_discount == subtotal` (HT après remise) ;
  - `designation` non vide, longueur maximale à valider ;
  - `type` ∈ `{"product", "service"}`.
- Les lignes à 0 (article gratuit, remise totale) doivent être traitées explicitement :
  - soit rejetées avec un motif clair ;
  - soit exclues du payload avec une règle documentée.
  - **Ne pas** les transformer en `quantity=1` ou `unit_price=1`.
- Ajouter les erreurs dans une liste `payload_errors` et les remonter via `validate_sfec_payload()` ou une exception `422`.

**Tests :** quantité 0, prix 0, taux 20, remise > sous-total, ligne gratuite, avoir négatif normalisé en positif.

### 5.4 Validation des totaux

**Fichier :** `app/integration/sfec/endpoints.py`

Compléter `validate_sfec_payload()` avec les contrôles suivants (tolérance `validation.tolerance_amount`) :

1. `total_tax_amount == total_tax_t_amount + total_tax_r_amount`.
2. `subtotal == somme(items.subtotal)`.
3. `total_line_discount_amount == somme(items.discount_amount)`.
4. `total_amount == subtotal + total_tax_amount + additional_cent_tax + somme(additional_taxes) + electronic_stamp_duty`.
5. `total_amount == somme(items.total_amount) + additional_cent_tax + somme(additional_taxes)`.
6. `amount_due <= total_amount` (sauf débours explicitement modélisés) et `amount_due >= 0`.
7. `electronic_stamp_duty == 0`.
8. `total_tax_t_amount`/`total_tax_r_amount` non négatifs.
9. `total_exempt_amount` non négatif.
10. `total_amount > 0`.
11. Cohérence `check_sage_totals()` conservée : `total_amount` reconstruit == `montant_ttc` Sage à la tolérance près.

**Tests :** `total_tax_amount=0` avec TVA 180, `total_ttc` incohérent, `subtotal` différent de la somme des lignes, remise globale, débours.

### 5.5 Longueurs et champs texte

**Fichier :** `app/integration/sfec/endpoints.py`

Ajouter une validation locale avant tout HTTP :

| Champ | Limite | Comportement |
|---|---:|---|
| `recipient_name` | 100 | rejet `422`, pas de troncature silencieuse |
| `invoice_subject` | 100 | troncature contrôlée ou omission |
| `notes` | 500 | troncature contrôlée avec avertissement |
| `payment_reference` | 100 | troncature contrôlée ou omission |
| `recipient_address` | 255 | rejet si trop long |
| `designation` | à confirmer OpenAPI | rejet/troncature contrôlée |
| `external_credit_note_number` | à confirmer | rejet si trop long |

- Ne plus recopier `reference` intégralement dans `invoice_subject`, `payment_reference` et `notes`.
- Utiliser un helper `_clip(value, max_len, field, errors)` qui tronque les champs non légaux et rejette les champs légaux.
- Un dépassement ne doit jamais produire un `500` SFEC.

**Tests :** `recipient_name` 100/101, `notes` 500/501, `reference` longue, adresse 255/256.

### 5.6 Destinataires : supprimer les valeurs factices

**Fichier :** `app/integration/sfec/endpoints.py`

- Supprimer l’usage production de :
  - `_DEFAULT_RECIPIENT_NIU = "M000000000000001"` ;
  - `_DEFAULT_RECIPIENT_PHONE` ;
  - `_DEFAULT_RECIPIENT_EMAIL`.
- `recipient_niu = raw_niu or comp_niu` ; si vide :
  - `business`/`government` → erreur `422` ;
  - `individual`/`foreign` → ne pas envoyer le champ.
- Ne plus convertir automatiquement `business` en `individual` faute de NIU. Le type doit rester celui de Sage, et l’absence de NIU doit être visible.
- `recipient_phone`/`recipient_email` : ne pas envoyer de valeur factice. Pour `foreign`, exiger téléphone + email (et adresse selon la doc) ; pour `government`, exiger les coordonnées selon la doc.
- Valider la longueur du NIU : 16 ou 17 caractères.
- Si un NIU invalide est fourni, ne pas utiliser le NIU compagnie par défaut : rejeter avec un motif clair.
- `recipient_name` : si absent, rejeter ; ne pas utiliser `"Client"` par défaut en production. Un fallback `"Client"` peut rester uniquement pour les tests.

**Tests :** business sans NIU, government sans NIU, individual sans NIU, foreign sans téléphone/email, NIU 16/17/15, NIU invalide.

### 5.7 `amount_due`

**Fichier :** `app/integration/sfec/endpoints.py`

- Le code force `amount_due = total_amount` si `montant_restant <= 0`. Vérifier si SFEC accepte `amount_due=0`.
- Si `0` est accepté : préserver `0` pour une facture soldée.
- Si `0` est refusé : documenter la règle et conserver le fallback, mais avec un avertissement explicite.
- Ne pas confondre “net à payer absent” et “net à payer nul”.

**Test sandbox :** facture certifiée avec `amount_due=0`, puis lecture du `GET`.

## 6. Lot 3 — Avoirs

### 6.1 `creditNoteOnAccount` : conception

**Fichiers :** `app/sync/engine.py`, `app/integration/sfec/endpoints.py`, `app/storage/db.py`, `app/web/routes/billing.py`, `app/web/templates/billing/form.html`

Décision de base :

- Un avoir **classique** référence une facture de vente certifiée (`creditNote`).
- Un avoir **sur compte** n’a pas de facture parente ; il porte une référence unique (`creditNoteOnAccount`).
- Un avoir avec une référence parente fournie mais introuvable **ne doit pas** devenir automatiquement on-account : c’est `parent_absent`.
- Seul un avoir sans référence parente, ou un avoir explicitement marqué “sur compte”, devient `creditNoteOnAccount`.

Implémentation :

1. Ajouter un champ explicite `credit_note_on_account` (bool) dans le dictionnaire facture.
2. Dans `_prepare_credit_note()` :
   - si `not ref` et `_is_credit_note(invoice)` :
     - `invoice["credit_note_on_account"] = True` ;
     - `invoice["reference_invoice_id"] = numero or id` ;
     - **ne pas** renseigner `original_sfec_invoice_number` ;
     - retourner `("ok", reference, None, "")`.
   - si `credit_note_on_account` est vrai et qu’une référence est fournie, l’utiliser sans chercher de parent.
   - si une référence parente est fournie et introuvable, rester `parent_absent`.
3. Dans `db_invoice_to_sfec()` :
   - `invoice_type = "creditNoteOnAccount"` si le flag est vrai ;
   - `reference_invoice_id` obligatoire ;
   - ne pas envoyer `external_credit_note_number`/`original_sfec_invoice_number` pour ce type (ou les envoyer seulement si explicitement fournis, après test sandbox).
4. Dans `validate_sfec_payload()` :
   - `creditNote` : `reference_invoice_id` + champs de traçabilité requis ;
   - `creditNoteOnAccount` : `reference_invoice_id` requis, pas de parent exigé.
5. Pour les factures locales :
   - ajouter la colonne `credit_note_on_account INTEGER DEFAULT 0` avec migration `_migrate()` ;
   - persister le champ dans `create_invoice`/`update_invoice` ;
   - ajouter une case “Avoir sur compte” dans le formulaire, visible seulement pour `type_doc=avoir` ;
   - si cochée, ne pas exiger `reference_invoice_id` ; utiliser le numéro de l’avoir comme référence par défaut ;
   - exposer le champ dans l’API JSON.
6. Pour Sage :
   - un avoir sans référence parente est automatiquement on-account ;
   - un avoir avec référence parente reste classique.

**Tests :**

- avoir sans référence → payload `creditNoteOnAccount`, `reference_invoice_id = numéro avoir`, pas de `original_sfec_invoice_number` ;
- avoir avec référence parente valide → `creditNote` classique ;
- avoir avec référence inconnue → `parent_absent`, pas de bascule on-account ;
- avoir sur compte avec référence explicite inconnue → accepté (l’API ne vérifie pas l’existence) ;
- doublon de référence → `409` traité comme rejet métier ;
- formulaire/API locale : case on-account, validation, persistance.

### 6.2 Avoir classique : partiels et champs

- Conserver les avoirs partiels : un parent 1180 → avoir 500 → avoir 680 → OK.
- `_build_credit_note_index` continue de clé par numéro d’avoir, pas par parent.
- `external_credit_note_number` = numéro de l’avoir ; `original_sfec_invoice_number` = numéro SFEC du parent.
- Ne pas exiger ces champs dans le formulaire : les dériver automatiquement.
- `validate_sfec_payload()` : si `creditNote` et champs absents après dérivation → `422` avec motif exploitable.

### 6.3 Avoir sur-soldé : `400` métier

**Fichiers :** `app/integration/sfec/client.py`, `app/integration/sfec/endpoints.py`, `app/sync/engine.py`

- Dans `SfecClient._request()`, parser le corps JSON des erreurs et attacher :
  - `exc.error_json` ;
  - `exc.error_message` ;
  - `exc.retry_after` pour `429`.
- Ajouter un helper `parse_overcredit_error(exc)` qui reconnaît un `400` avec `already_credited`, `invoice_net`, `remaining`.
- Centraliser la classification des erreurs dans `engine.py` :

```python
def _classify_certify_error(exc):
    # -> ("overcredit", details) / ("already_certified", None)
    #    ("rejected", reason) / ("transient", None) / ("server", None)
```

- Utiliser cette classification dans :
  - `_certify_single_worker()` ;
  - `_process_retry_queue()` ;
  - `certify_single()` ;
  - `certify_sqlite_invoice()`.
- Sur overcredit :
  - `mark_to_monitor(invoice_id, "Avoir sur-soldé: déjà crédité X, reste Y (facture Z)")` ;
  - `_reset_retry(numero)` ;
  - retour `error_overcredit` ou `success=False` ;
  - **aucun retry automatique** tant que l’avoir n’est pas corrigé.
- Ne pas confondre avec un `409` de doublon d’`invoice_id`.

**Tests :** faux `400` avec `already_credited=1180`, `remaining=0` ; vérifier le motif, l’absence de retry et le statut `A_SURVEILLER`.

### 6.4 `409` sur `creditNoteOnAccount`

- Pour un `creditNote` classique : `409` = `invoice_id` déjà utilisé → `_handle_already_certified()` via `get_invoice(numero)`.
- Pour un `creditNoteOnAccount` : `409` peut signifier “référence déjà utilisée” avec un autre `invoice_id`.
  - Si le corps contient `reference`, ne pas tenter de récupération par `invoice_number` ;
  - marquer `A_SURVEILLER` avec `Référence d'avoir sur compte déjà utilisée: <ref>` ;
  - ne pas écrire `DEJA_CERTIFIE`.
- Si le corps contient `invoice_id` déjà utilisé, appliquer la récupération classique.

**Tests :** `409` référence on-account, `409` invoice_id classique, `409` avec corps illisible.

## 7. Lot 4 — Robustesse réseau et performance

### 7.1 `429`, `5xx`, timeout

**Fichier :** `app/integration/sfec/client.py`

- `_is_retryable_verify_error()` : inclure `429` et `5xx`.
- Lire `Retry-After` (secondes ou date HTTP) et l’exposer sur l’exception.
- Backoff : `2 s`, `5 s`, puis `Retry-After` si présent, avec plafond configurable.
- `GET` : 3 tentatives.
- `POST` : ne pas retenter aveuglément dans `_request()` ; laisser le moteur gérer `429`/`5xx` avec backoff, en s’appuyant sur l’idempotence de `invoice_id` et sur `_handle_already_certified()` pour les `409`.
- Configurer `sfec.timeout` (défaut 30 s) et `sfec.max_retries`.
- Tester `429` avec et sans `Retry-After`, `503`, timeout, `ConnectionError`.

### 7.2 Lookup et performances

- `_build_sfec_lookup()` : `page_size=20`, `totalPages`, cache 5 min.
- Ajouter des logs de durée et du nombre de pages.
- Si le nombre de pages dépasse `sfec.lookup_max_pages` :
  - utiliser `date_start`/`date_end` pour scanner par fenêtres ;
  - ou basculer sur une vérification à la demande des factures à certifier.
- `SfecClient.list_invoices()` : utiliser `urllib.parse.urlencode` pour encoder `date_start`/`date_end` (les `+` de fuseau ne doivent pas devenir des espaces).
- `app/web/routes/config.py` : `page_size=100` → `20` + pagination si un écran doit tout voir.

### 7.3 Tri et exports

- Le tri SFEC par `invoice_date` n’est pas stable : pour un export exhaustif, utiliser des fenêtres `date_start`/`date_end`.
- Trier localement par `(invoice_date, created_at, id)` avant comparaison.
- Documenter que `page` au-delà de la dernière page est renvoyé tel quel.

## 8. Lot 5 — Données, UI et documentation

### 8.1 `items_json` et champs à zéro

- L’API omet certains champs quand ils valent zéro.
- Tout lecteur doit utiliser `.get()` avec valeurs par défaut.
- Ajouter un helper `normalize_sfec_item(item)` si des écrans/exportent lisent `items_json`.
- Test : item avec `unit_price=0`/`quantity=0` ne doit pas provoquer `KeyError`.

### 8.2 Débours

**Si le client facture des débours :**

- Ajouter `is_debours` sur les lignes et `total_debours` sur la facture.
- Alimenter depuis Sage (champ à identifier avec le client) et/ou le formulaire local.
- Dans le payload :
  - `total_debours` séparé ;
  - `amount_due` et `total_amount` hors débours ;
  - net dû = `amount_due + total_debours`.
- Validation : `total_debours >= 0`, lignes débours exclues du HT/TVA.
- Tests de contrat et sandbox.
- Si le client n’en facture pas : documenter explicitement que le champ n’est pas supporté et ne pas ajouter de complexité inutile.

### 8.3 Documentation

- Mettre à jour `docs/` avec :
  - les champs de certification réellement exposés (`certification_signature`, `certification_short_signature`) ;
  - la différence `creditNote` / `creditNoteOnAccount` ;
  - le mapping des moyens de paiement ;
  - les formats de date RFC 3339 ;
  - la politique de validation locale ;
  - les limites de longueur.
- Ajouter ce plan et le rapport sandbox dans l’index de `docs/checking/` si un index existe.

## 9. Tests et critères d’acceptation

### 9.1 Fichiers de tests

| Fichier | Cas |
|---|---|
| `tests/test_sfec_client.py` | `get_invoice`, URL encoding, `verify_by_invoice_number`, champs de certification, `404`, `429`, `5xx`, timeout |
| `tests/test_sfec_payload_validation.py` | dates, paiement, taux 0/5/18/20, quantité 0, prix 0, remises, totaux, longueurs, destinataires |
| `tests/test_sfec_credit_notes.py` | classique, partiel, sur-soldé, on-account, doublon de référence, `409` |
| `tests/test_sage_odbc_409.py` | mocks `get_invoice` ; suppression de `page_size=500`/`total` |
| `tests/test_sfec_avoirs.py` | adapter le test “avoir sans référence” vers on-account |
| `tests/test_avoirs.py` | validation route locale + case on-account |
| `tests/perf/mocks/sfec.py` | `get_invoice`, `page_size=20` |
| `tests/test_sfec_sandbox_integration.py` | tests live optionnels : date RFC 3339, paiement, on-account, avoir partiel |

### 9.2 Commandes de validation

```bash
# Linux
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m compileall -q app main.py
git diff --check

# Windows
"%VENV_PY%" -m pytest tests/ -q
"%VENV_PY%" -m compileall -q app main.py
```

Tests sandbox :

```bash
TCONNECTOR_TEST_SFEC_SANDBOX=1 .venv/bin/python -m pytest tests/test_sfec_sandbox_integration.py -q
```

### 9.3 Definition of Done

- [ ] Aucun appel à `page_size=500` ni lecture de `total`/`total_count`.
- [ ] `verify_by_invoice_number()` retrouve une facture en page 2 sans scan complet.
- [ ] `verify_certification()` retourne `True` sur une facture certifiée.
- [ ] `invoice_due_date`/`payment_date` sont stockés en RFC 3339.
- [ ] Les taux, quantités, prix, remises et totaux incohérents produisent un `422` local avec motif.
- [ ] Aucun NIU/téléphone/email factice n’est envoyé en production.
- [ ] `creditNoteOnAccount` est envoyé pour un avoir sans parent et pour un avoir local explicitement marqué.
- [ ] Un avoir sur-soldé produit `A_SURVEILLER` avec `remaining`, sans retry.
- [ ] Un `429` respecte `Retry-After` et ne provoque pas de tempête.
- [ ] Les tests historiques sont adaptés, pas contournés.
- [ ] Les tests sandbox passent avec une clé valide, hors CI par défaut.
- [ ] Documentation mise à jour.

## 10. Séquencement, risques et rollback

### 10.1 Séquence recommandée

| Phase | Contenu | Dépendance | Estimation |
|---|---|---|---|
| 0 | Préparation, tests, fixtures | — | 0,5 j |
| 1 | Lot 1 : récupération/vérification | 0 | 0,5–1 j |
| 2 | Lot 2 : payload/validation | 1 | 1–1,5 j |
| 3 | Lot 3 : avoirs | 1, 2 | 1–2 j |
| 4 | Lot 4 : réseau/perf | 1 | 0,5–1 j |
| 5 | Lot 5 : données/UI/docs | 2, 3 | 0,5–1 j |
| 6 | Régression + sandbox + revue | toutes | 0,5–1 j |

### 10.2 Risques

| Risque | Mitigation |
|---|---|
| Blocage d’avoirs valides à cause d’une validation trop stricte | Tests sandbox ; config `validation` ; décisions métier explicites |
| Mapping de paiement Sage inconnu | Table configurable + rejet explicite plutôt que fallback silencieux |
| `creditNoteOnAccount` mal détecté | Uniquement sans parent ou flag explicite ; jamais sur `parent_absent` |
| Lookup trop long | Cache, `totalPages`, `max_pages`, fenêtres de dates |
| Double certification après retry | `_handle_already_certified` par `get_invoice`, idempotence `invoice_id` |
| Régression sur les exports | Tests de non-régression + tri local stable |
| Clé sandbox dans les tests | `SFEC_SANDBOX_API_KEY`/config non versionnée ; fixtures anonymisées |

### 10.3 Rollback

- Chaque lot est indépendant et peut être reverti séparément.
- Le lot 1 (client) est le plus sûr à déployer seul : il améliore la récupération sans changer le payload.
- Les lots 2 et 3 doivent être déployés ensemble si les validations bloquent des factures existantes.
- En cas de blocage massif, prévoir un interrupteur `validation.strict=false` temporaire, mais **ne pas** désactiver la validation des totaux en production sans décision explicite.

## 11. Décisions à valider

1. **`mixte`** : rejet local, mapping vers `credit`, ou configuration explicite ?
2. **Lignes à 0** : rejet, exclusion, ou support d’un cas “article gratuit” ?
3. **`government`/`foreign`** : appliquer strictement l’OpenAPI (NIU + coordonnées) ou reproduire la permissivité observée du sandbox ?
4. **`amount_due=0`** : SFEC l’accepte-t-il ? Si non, conserver le fallback actuel ?
5. **Débours** : le client en facture-t-il ? Si oui, quel champ Sage les porte ?
6. **`creditNoteOnAccount`** : le client utilise-t-il des avoirs sans facture parente ? La case locale est-elle nécessaire ou seul le flux Sage suffit-il ?
7. **`original_sfec_invoice_number`** : le conserver comme exigence de traçabilité (dérivé automatiquement) ou l’aligner sur l’API qui l’accepte absent ?
8. **`verify_certification()`** : la conserver comme API publique ou la supprimer si elle n’est pas utilisée ?

## 12. Annexe — checklist par fichier

### `app/integration/sfec/client.py`

- [ ] `get_invoice()` encode l’identifiant.
- [ ] `verify_by_invoice_number()` appelle directement `get_invoice`.
- [ ] Suppression de la pagination `page_size=500`/`total`.
- [ ] `verify_certification()` lit `certification_short_signature`/`certification_signature`.
- [ ] `429`/`Retry-After`, `5xx`, timeout.
- [ ] `list_invoices()` encode les paramètres.

### `app/integration/sfec/endpoints.py`

- [ ] `_normalize_datetime()` RFC 3339.
- [ ] Mapping paiement unique.
- [ ] Validation quantité/prix/taux/remise.
- [ ] Validation totaux T/R, lignes, `total_amount`, `amount_due`.
- [ ] Validation longueurs.
- [ ] Suppression des NIU/téléphone/email factices.
- [ ] `creditNoteOnAccount`.
- [ ] `validate_sfec_payload()` branches classic/on-account.
- [ ] Parsing des erreurs SFEC (`error_json`, overcredit).

### `app/sync/engine.py`

- [ ] `_build_sfec_lookup()` `totalPages` + `page_size=20`.
- [ ] `_prepare_credit_note()` on-account sans parent.
- [ ] Classification centralisée des erreurs.
- [ ] Overcredit `400` → `A_SURVEILLER`, sans retry.
- [ ] `409` on-account vs classic.
- [ ] `creditNoteOnAccount` dans les chemins de certification.

### `app/storage/db.py`

- [ ] Colonne `credit_note_on_account` + migration.
- [ ] Persistance dans `create_invoice`/`update_invoice`.
- [ ] Champs débours si retenus.

### `app/web/routes/billing.py` et templates

- [ ] Case “Avoir sur compte”.
- [ ] Validation route : référence non requise si on-account.
- [ ] Affichage du type d’avoir.
- [ ] Longueurs côté formulaire (optionnel, la validation serveur reste obligatoire).

### Tests

- [ ] `test_sfec_client.py`.
- [ ] `test_sfec_payload_validation.py`.
- [ ] `test_sfec_credit_notes.py`.
- [ ] `test_sage_odbc_409.py` adapté.
- [ ] `test_sfec_avoirs.py` adapté.
- [ ] `test_avoirs.py` on-account.
- [ ] Mocks perf alignés.
- [ ] Tests sandbox optionnels.

---

**Conclusion :** le correctif le plus urgent et le plus rentable est le lot 1 : remplacer la recherche paginée par `GET /api/v1/invoices/{invoice_number}` et corriger `_build_sfec_lookup`. Il débloque la récupération des certifications et la résolution des parents d’avoirs. Les lots 2 et 3 sécurisent ensuite les payloads et les avoirs ; les lots 4 et 5 traitent la robustesse et les cas avancés.
