# Rapport d’observations — API SFEC (sandbox)

**Date du rapport :** 28/10/2026  
**Environnement testé :** `https://sandbox.api.sfec.gouv.cg`  
**Sources :** documentation officielle `docs.sfec.gouv.cg`, OpenAPI `openapi.yaml` (OpenAPI 3.0.3, API 1.0.0), collection Insomnia `insomnia-sfec-workflow.yaml`, code `app/integration/sfec/`  
**Statut :** tests réalisés dans le sandbox, aucune modification de code, aucune donnée de production utilisée  
**Clé API :** non reproduite dans ce document ; elle doit être fournie via `SFEC_SANDBOX_API_KEY`

## 1. Résumé exécutif

L’API SFEC fonctionne et la clé sandbox fournie permet les opérations `GET` et `POST`. Les tests ont toutefois mis en évidence un point majeur : **l’API est beaucoup plus permissive que sa documentation**. Elle accepte plusieurs valeurs que l’OpenAPI déclare invalides, ne réconcilie pas toujours les totaux et renvoie parfois des `500` au lieu de `400`.

Les observations les plus importantes pour TConnector sont :

1. **La recherche d’une facture par numéro est cassée dans le connecteur.** `verify_by_invoice_number()` ne parcourt que les 20 premières factures à cause de `pageSize=500` ramené à 20 et de l’utilisation de `total` au lieu de `totalPages`. Les factures en page 2 ne sont pas trouvées.
2. **Le endpoint de détail accepte directement le numéro de facture.** La correction la plus simple est d’appeler `GET /api/v1/invoices/{invoice_number}` au lieu de paginer la liste.
3. **`verify_certification()` lit les mauvais champs.** Le `GET` renvoie `certification_signature` et `certification_short_signature`, pas `certification_number` ni `short_signature`.
4. **`creditNoteOnAccount` n’est pas supporté par le connecteur**, alors que l’API le gère.
5. **Les dates doivent être envoyées en RFC 3339 complet.** Le connecteur envoie `YYYY-MM-DD` ; l’API accepte le payload mais stocke `null`.
6. **L’API ne valide pas assez les données métier.** Elle accepte `unit_price=0`, `quantity=0`, `tax_rate=20`, `payment_method="1"` et des totaux incohérents. Le connecteur doit jouer le rôle de garde-fou.
7. **Un avoir sur-soldé renvoie `400`, pas `409`.** Le moteur doit gérer les champs `already_credited`, `invoice_net` et `remaining`.

## 2. Périmètre et méthode

Les tests ont porté sur :

- l’authentification et les erreurs `401` ;
- la liste paginée et les filtres ;
- le détail par UUID et par numéro de facture ;
- la certification de factures de vente dans plusieurs configurations fiscales ;
- les remises, centimes, taxes additionnelles et débours ;
- les devises, types de destinataire, dates et moyens de paiement ;
- les avoirs classiques et les avoirs sur compte ;
- la reproduction des fonctions de recherche et de vérification du connecteur.

Environ **31 factures/avoirs de test** ont été créés dans le sandbox. Ils ne peuvent pas être supprimés via l’API, qui n’expose pas de `DELETE`. Aucune donnée de production n’a été utilisée.

## 3. Contrat API observé

### 3.1 Authentification et environnements

- Production : `https://api.sfec.gouv.cg`
- Sandbox : `https://sandbox.api.sfec.gouv.cg`
- Header obligatoire : `X-API-Key: <clé>`
- La clé est liée à un `business_id` : seules les factures de ce contribuable sont visibles.
- `401` sans clé : `{"message":"API key is required"}`
- `401` avec clé invalide : `{"message":"Invalid API key"}`

### 3.2 `GET /api/v1/invoices`

Réponse observée :

```json
{
  "invoices": [],
  "totalPages": 53,
  "page": 1,
  "pageSize": 10
}
```

Points confirmés :

- pas de champ `total` ni `total_count` ;
- `pageSize` par défaut : `10` ;
- `pageSize < 1` → `10` ;
- `pageSize > 20` → `20` ;
- `page < 1` → `1` ;
- `page` au-delà de la dernière page est renvoyé tel quel, avec une liste vide ;
- `invoice_type` accepte `salesInvoice`, `creditNote`, `creditNoteOnAccount` ; une valeur inconnue renvoie `200` avec une liste vide, pas `400` ;
- `date_start` et `date_end` doivent être en RFC 3339 complet ;
- une date seule ou invalide est ignorée silencieusement ;
- `date_end` est arrondi à la fin de la journée dans le fuseau transmis ;
- tri par `invoice_date` décroissante, sans critère de départage stable.

Exemples observés :

| Requête | `page` | `pageSize` | `totalPages` | Éléments |
|---|---:|---:|---:|---:|
| défaut | 1 | 10 | 53 | 10 |
| `page=1&pageSize=1` | 1 | 1 | 530 | 1 |
| `page=0&pageSize=0` | 1 | 10 | 53 | 10 |
| `page=-5&pageSize=500` | 1 | 20 | 27 | 20 |
| `page=1&pageSize=21` | 1 | 20 | 27 | 20 |
| `page=9999&pageSize=20` | 9999 | 20 | 27 | 0 |

### 3.3 `GET /api/v1/invoices/{id}`

- Accepte un UUID ou un `invoice_number`.
- Retourne exactement les mêmes champs qu’un élément de la liste.
- `404` si la facture n’existe pas ou n’appartient pas au `business_id`.
- Champs de certification disponibles :
  - `certification_signature`
  - `certification_short_signature`
  - `certification_qr_code`
  - `certification_date`
  - `certification_status`
- Les champs `certification_number`, `short_signature` et `signature` ne sont **pas** présents dans le `GET`.

### 3.4 `POST /api/v1/invoices`

Champs obligatoires principaux :

```text
invoice_id, invoice_type, subtotal,
total_tax_t_amount, total_tax_r_amount, total_exempt_amount,
total_tax_amount, discount_amount, amount_due,
total_line_discount_amount, additional_cent_tax,
electronic_stamp_duty, total_amount, currency,
recipient_type, is_recipient_taxable, items
```

Sémantique fiscale confirmée par la documentation et les tests :

- `total_tax_t_amount` : TVA au taux normal 18 % ;
- `total_tax_r_amount` : TVA au taux réduit 5 % ;
- `total_tax_amount` : `total_tax_t_amount + total_tax_r_amount` ;
- `total_exempt_amount` : montant exonéré ;
- `discount_amount` : remise globale, appliquée uniquement à `amount_due` ;
- `total_line_discount_amount` : somme des remises de ligne ;
- `additional_cent_tax` : taxe additionnelle en centimes ;
- `electronic_stamp_duty` : doit être `0` ;
- `amount_due` : montant commercial à payer, **hors débours** ;
- `total_debours` : cumul des débours, hors TVA et hors chiffre d’affaires ;
- le net réellement dû est `amount_due + total_debours`.

Contraintes d’article :

- `designation` non vide ;
- `type` : `product` ou `service` ;
- `unit_price >= 1` dans l’OpenAPI ;
- `quantity >= 1` dans l’OpenAPI ;
- `subtotal` : HT après remise de ligne ;
- `discount_amount` : remise de ligne ;
- `amount_after_discount` : HT après remise ;
- `net_amount` : HT après remise (observé) ;
- `tax_rate` : `"0"`, `"5"` ou `"18"` ;
- `total_amount` : TTC de la ligne.

Destinataires :

- `recipient_type` : `business`, `individual`, `government`, `foreign` ;
- `recipient_niu` : 16 ou 17 caractères, requis pour `business` et `government` selon la doc ;
- `recipient_name` : requis pour `business`, `government` et `foreign` ;
- `recipient_phone`, `recipient_email`, `recipient_address` : conditionnels pour `government` et `foreign` ;
- pour `individual`, ces champs sont optionnels.

Avoirs :

- `creditNote` :
  - `reference_invoice_id` obligatoire ;
  - la facture parente doit exister, être certifiée et ne pas être un avoir ;
  - `external_credit_note_number` et `original_sfec_invoice_number` sont optionnels ;
- `creditNoteOnAccount` :
  - `reference_invoice_id` obligatoire et non vide ;
  - la référence n’est pas recherchée comme facture ;
  - unicité par contribuable ; doublon → `409`.

### 3.5 Réponse `201`

Sept champs toujours présents :

```json
{
  "certification_number": "...",
  "signature": "...",
  "short_signature": "...",
  "identifier": "...",
  "invoice_number": "...",
  "qr_code": "data:image/png;base64,...",
  "certification_date": "..."
}
```

Relations confirmées :

- `certification_number` = `signature` : 64 caractères hexadécimaux minuscules ;
- `identifier` = `short_signature` : 20 caractères majuscules ;
- `invoice_number` = l’`invoice_id` transmis ;
- `qr_code` = data URI PNG en base64 ;
- `certification_date` = RFC 3339.

### 3.6 Erreurs

| Code | Cas |
|---|---|
| `400` | validation, parent d’avoir manquant/invalide, avoir sur avoir, avoir sur-soldé |
| `401` | clé absente/invalide, NIU vendeur inconnu |
| `404` | facture référencée introuvable pour `creditNote` |
| `409` | `invoice_id` déjà utilisé, référence `creditNoteOnAccount` déjà utilisée |
| `429` | quota dépassé (non testé) |
| `500` | erreur interne, notamment certaines longueurs dépassées |

## 4. Résultats des tests

### 4.1 Authentification

| Test | Résultat |
|---|---|
| Clé sandbox fournie | `200` |
| Sans clé | `401 API key is required` |
| Clé invalide | `401 Invalid API key` |
| `POST` sans clé / clé invalide | `401` |

### 4.2 Liste et pagination

Les tests confirment que :

- `pageSize=500` est ramené à `20` ;
- la réponse contient `totalPages`, pas `total` ;
- `page=9999` est renvoyé tel quel ;
- `invoice_type=badType` renvoie `200` et une liste vide ;
- les filtres de date en RFC 3339 fonctionnent ;
- les dates seules ou invalides sont ignorées.

### 4.3 Détail

- Détail par UUID : `200`.
- Détail par `invoice_number` : `200`.
- Détail inconnu : `404 {"message":"invoice not found"}`.
- Le `GET` contient `certification_signature` et `certification_short_signature`.
- Il contient aussi `items_json`, `status`, les champs `buyer_*`, `seller_*`, `additional_taxes`, etc.

### 4.4 Validation `POST`

Cas correctement rejetés :

| Cas | Résultat |
|---|---|
| Corps vide | `400` avec erreurs `REQUIRED` |
| `items` absent ou vide | `400` |
| Email invalide | `400 INVALID_FORMAT` |
| Devise `GBP` | `400 INVALID_VALUE` |
| `invoice_type=badType` | `400 INVALID_VALUE` |
| `invoice_id` déjà utilisé | `409` |
| `business` sans NIU | `400 REQUIRED` |
| `foreign` sans téléphone/email | `400 REQUIRED` |
| `creditNote` sans référence | `400 REQUIRED` |
| `creditNote` avec parent inexistant | `404` |
| `creditNote` sur un avoir | `400` |
| `creditNoteOnAccount` sans référence | `400 REQUIRED` |
| `recipient_name` de 101 caractères | `500` |

Cas anormalement acceptés :

| Cas | Résultat | Effet observé |
|---|---|---|
| `unit_price=0` | `201` | facture certifiée ; `items_json` omet `unit_price`, `subtotal`, `net_amount`, `tax_amount`, `total_amount` |
| `quantity=0` | `201` | facture certifiée ; `items_json` omet `quantity` |
| `tax_rate="20"` | `201` | `tax_rate` stocké à `"20"` |
| `payment_method="1"` | `201` | `payment_method` stocké à `"1"` |
| `invoice_due_date="2026-12-31"` | `201` | `invoice_due_date` stocké à `null` |
| `total_tax_amount=0` avec 180 de TVA | `201` | `tva=0` alors que `total_tax18=180` |
| `total_ttc` incohérent avec `total_ht` + TVA | `201` | valeurs incohérentes conservées |
| `amount_due=500` pour un total de 1180 | `201` | paiement partiel accepté |
| NIU factice `M000000000000001` | `201` | NIU accepté et stocké |
| `government` sans NIU | `201` | `buyer_niu=null` |

### 4.5 Fiscalité, remises et totaux

| Cas | Résultat |
|---|---|
| TVA 18 % | `total_tax18=180`, `total_ttc=1180` |
| TVA 5 % | `total_tax5=50`, `total_ttc=1050` |
| Exonéré 0 % | `total_exempt=1000`, `total_ttc=1000` |
| Mixte 18 % + 5 % + exonéré | `total_ht=3000`, `total_ttc=3230` |
| Remise ligne 100 | `total_ht=900`, `total_tax18=162`, `total_ttc=1062` |
| Remise globale 50 | `overall_discount=50`, `amount_due=1012` |
| Taxe centimes 9 | `additional_tax5=9`, `total_ttc=1189` |
| Taxes additionnelles 10 | `additional_taxes` restitué, `amount_due=1190` |
| Débours 500 | `total_debours=500`, `amount_due=1180` ; net dû = 1680 |
| Devises USD/EUR | acceptées et restituées |
| Dates RFC 3339 | conservées |
| Paiement partiel | `amount_due=500`, `total_ttc=1180` |

L’API **ne recalcule pas** systématiquement les totaux. Elle stocke notamment `tva` à partir de `total_tax_amount` et `total_ttc` à partir de `total_amount`, sans vérifier la cohérence avec `total_ht`, les articles ou `total_tax18`/`total_tax5`.

### 4.6 Destinataires et moyens de paiement

- `business` avec NIU valide : accepté.
- `business` sans NIU : `400`.
- `government` sans NIU : **accepté** (écart avec la doc).
- `foreign` sans téléphone/email : `400`.
- `foreign` sans adresse : accepté ; l’adresse n’est pas obligatoire.
- `individual` sans NIU : accepté.
- `payment_method` : `bank_transfer`, `mobile_money`, `credit` acceptés.
- `payment_method="1"` : accepté et stocké tel quel.
- `recipient_name` 100 caractères : accepté.
- `recipient_name` 101 caractères : `500`.
- `notes` 500, `invoice_subject` 100, `delivery_address`/`transaction_location` 255 : acceptés.

### 4.7 Avoirs

#### `creditNote`

- Avoir partiel autorisé : parent 1180 → avoir 500, puis avoir 680 → `201`.
- Troisième avoir de 1 → `400` :

```json
{
  "already_credited": "1180",
  "invoice_net": "1180",
  "remaining": "0",
  "error": "credit notes would total 1181, above the invoice net amount 1180 (0 left)"
}
```

- Doublon du même `invoice_id` alors qu’il reste du crédit → `409`.
- Avoir sur un avoir → `400 "cannot create a credit note for a credit note"`.
- Parent inexistant → `404`.
- Avoir avec seulement `reference_invoice_id` → `201` ; `external_credit_note_number` et `original_sfec_invoice_number` restent `null`.

#### `creditNoteOnAccount`

- `reference_invoice_id` obligatoire et non vide.
- Aucune vérification d’existence de la référence.
- Doublon de référence → `409` :

```json
{
  "error": "a credit note on account with this reference already exists",
  "reference": "AV-COMPTE-..."
}
```

- Doublon d’`invoice_id` → `409`.
- Le `GET` stocke la référence externe dans `reference_invoice_id`, avec `original_sfec_invoice_number=null`.

### 4.8 Performance

- premier appel à froid : jusqu’à **10,9 s** ;
- appels suivants : environ **1 s** ;
- page de 20 factures : **1,4 à 4 s**, environ **78 Ko** à cause des QR codes base64 ;
- détail : environ **1 s** après chauffe.

Le timeout actuel de 30 s reste utilisable, mais il faut surveiller les appels à froid et éviter de scanner toutes les pages pour retrouver une facture.

## 5. Écarts entre la documentation et le comportement réel

| Sujet | Documentation / OpenAPI | Comportement observé |
|---|---|---|
| `unit_price` | minimum 1 | `0` accepté |
| `quantity` | minimum 1 | `0` accepté |
| `tax_rate` | `0/5/18` | `20` accepté |
| `payment_method` | enum fermé | `"1"` accepté et stocké |
| `government` sans NIU | requis | accepté avec `buyer_niu=null` |
| `foreign` sans adresse | adresse conditionnelle | adresse non exigée |
| `recipient_name` > 100 | `400 INVALID_LENGTH` attendu | `500` |
| `invoice_due_date` date seule | format date-time | accepté puis stocké `null` |
| `creditNote` déjà soldé | `409` | `400` avec `already_credited`/`remaining` |
| Totaux incohérents | validation attendue | `201` et valeurs incohérentes conservées |
| `GET` liste `invoice_type` inconnu | enum | `200` avec liste vide |
| `GET` liste `page` hors plage | corrigé à 1 | `page` renvoyé tel quel |
| Champs de certification `GET` | `certification_*` | `certification_signature` / `certification_short_signature` |

## 6. Impacts sur TConnector

### 6.1 Critique — recherche par numéro de facture

`SfecClient.verify_by_invoice_number()` :

- appelle `list_invoices(page_size=500)` ;
- l’API ramène à `20` ;
- lit `total`/`total_count`, absents ;
- s’arrête après la page 1.

Reproduction réelle :

```text
verify_by_invoice_number('25VFA3142') -> NOT FOUND
verify_by_invoice_number('9FAZ00123') -> NOT FOUND
verify_by_invoice_number('26FAZ00149') -> NOT FOUND
```

Ces factures existent en page 2.

**Correction recommandée :** utiliser directement `GET /api/v1/invoices/{invoice_number}` via `get_invoice(invoice_number)`. Le endpoint de détail accepte le numéro de facture et renvoie `404` s’il est absent. Cela supprime la pagination, le tri instable et les appels multiples.

### 6.2 `verify_certification()` cassé

```text
verify_certification('FCDFG013') -> False
```

Alors que le `GET` contient bien `certification_signature` et `certification_short_signature`.

Le code lit `certification_number` et `short_signature`, qui n’existent pas dans le `GET`.

### 6.3 Autres impacts

- `creditNoteOnAccount` absent de `_TYPE_DOC_TO_SFEC`.
- `_normalize_date()` renvoie `YYYY-MM-DD` ; l’API stocke `null`. Il faut envoyer du RFC 3339.
- `payment_method` est envoyé brut depuis Sage ; il faut mapper vers l’enum SFEC.
- L’API accepte des taux, prix et quantités invalides ; le connecteur doit valider avant l’envoi.
- L’API accepte des totaux incohérents ; le connecteur doit vérifier `total_tax_amount`, `total_ttc`, `amount_due` et les remises.
- Les dépassements de longueur peuvent produire un `500` ; il faut tronquer ou rejeter localement.
- Un avoir sur-soldé renvoie `400` ; le moteur doit gérer `already_credited`/`remaining` en plus du `409`.
- `validate_sfec_payload()` exige `original_sfec_invoice_number` pour un avoir, alors que l’API l’accepte absent. Ce n’est pas bloquant si le parent est connu, mais c’est plus strict que SFEC.
- Le NIU par défaut `M000000000000001` est accepté par le sandbox, mais il ne faut pas inventer un NIU en production.
- Le tri de la liste n’est pas stable ; pour un export exhaustif, utiliser `date_start`/`date_end`.
- `items_json` peut omettre les champs à zéro ; le code qui lit les articles doit utiliser `.get()` et ne pas supposer leur présence.

## 7. Données de test créées

Les tests ont créé **31 factures/avoirs** dans le sandbox. L’API n’expose pas de `DELETE` : ils restent en place.

| Préfixe / plage | Nombre | Type |
|---|---:|---|
| `VAL-*` | 5 | ventes de test, dont plusieurs anomalies |
| `AV-CLASSIC-*`, `AV-ACCOUNT-*` | 2 | avoir classique et avoir sur compte |
| `TCONN-1793191790-*` | 5 | TVA 18/5/0, mixte, remise |
| `TCONN-1793191901-*` | 10 | remise, centimes, taxes, débours, devises, destinataires, dates, longueurs |
| `TCONN-CN-*` | 3 | parent et avoirs partiels |
| `TCONN-*-1793192028` | 3 | government sans NIU, parent et avoir optionnel |
| `TCONN-*-1793192099` | 3 | NIU factice, paiement partiel, total de taxe incohérent |

Les cas rejetés (`400`/`500`) n’ont pas créé de facture, notamment :

- `VAL-NAME-1793191072` (nom trop long, `500`) ;
- `TCONN-BUS-NONIU-1793192028` (`400`) ;
- `TCONN-FOR-NOFIELDS-1793192028` (`400`) ;
- `TCONN-AC-NOREF-1793192028` (`400`).

## 8. Recommandations priorisées

### P0 — à corriger avant la prochaine campagne de certification

1. Remplacer la recherche paginée par `get_invoice(invoice_number)`.
2. Corriger `verify_certification()` ou supprimer cette méthode si elle est inutilisée.
3. Envoyer les dates en RFC 3339 complet.
4. Valider localement les totaux, taux, prix, quantités et remises.

### P1 — fonctionnel

5. Ajouter `creditNoteOnAccount` au mapping et à l’interface.
6. Mapper les moyens de paiement Sage vers l’enum SFEC.
7. Gérer le `400` métier des avoirs sur-soldés (`already_credited`, `remaining`).
8. Valider/tronquer les longueurs avant l’envoi pour éviter les `500`.
9. Ne pas utiliser de NIU factice ; exiger ou qualifier l’identité du destinataire.

### P2 — robustesse

10. Utiliser des fenêtres de dates pour les exports, à cause du tri instable.
11. Ajouter des tests de non-régression sur les champs `certification_*`.
12. Ajouter des tests de contrat avec un mock local et des tests live sandbox.
13. Documenter les différences doc/API dans le guide d’intégration.

## 9. Tests restants

Non couverts dans cette campagne :

- quota `429` et en-têtes de limitation ;
- reprise réelle après `5xx`/timeout ;
- tous les `payment_method` valides ;
- récupération complète après `409` via `certify_sqlite_invoice()` ;
- seuils exacts de longueur au-delà de 100/500/255 ;
- règles complètes `government`/`foreign` ;
- comportement du NIU vendeur (`taxpayer_niu`) invalide ;
- certification d’un backlog et impact de la date de certification.

## 10. Conclusion

L’API SFEC est utilisable, mais elle ne doit pas être considérée comme une source de validation métier. Le connecteur doit calculer, contrôler et normaliser les données avant l’envoi.

Le bug de pagination est le plus urgent : il empêche la récupération fiable des certifications existantes et la résolution des parents d’avoirs. La correction par appel direct au détail (`GET /api/v1/invoices/{invoice_number}`) est simple, rapide et robuste.

---

## Annexe A — reproduction

Les commandes ci-dessous utilisent une variable d’environnement, sans écrire la clé dans le dépôt :

```bash
export SFEC_SANDBOX_API_KEY='...'
export SFEC_BASE='https://sandbox.api.sfec.gouv.cg'

curl -sS \
  -H "X-API-Key: $SFEC_SANDBOX_API_KEY" \
  -H 'Accept: application/json' \
  "$SFEC_BASE/api/v1/invoices?page=1&pageSize=20"

curl -sS \
  -H "X-API-Key: $SFEC_SANDBOX_API_KEY" \
  -H 'Accept: application/json' \
  "$SFEC_BASE/api/v1/invoices/FCDFG013"
```

Pour une certification :

```bash
curl -sS -X POST \
  -H "X-API-Key: $SFEC_SANDBOX_API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  --data @payload.json \
  "$SFEC_BASE/api/v1/invoices"
```

Ne jamais committer `payload.json` s’il contient des données client ou la clé API.
