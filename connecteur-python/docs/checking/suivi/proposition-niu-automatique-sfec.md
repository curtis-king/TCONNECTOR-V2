# Proposition — Traitement automatique du NIU à la certification SFEC

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/checking/suivi/`
> **Statut** : IMPLÉMENTÉ — 2026-10-29
> **Périmètre** : NIU uniquement. Aucune modification de la base SQLite, aucune
> modification de la base Sage, aucun mapping de codes de taxe.

---

## 1. Décision

On retient l'option 1 déjà validée :

- un client comptoir / anonyme est traité comme **particulier** (`individual`) ;
- **aucun NIU n'est envoyé** pour ce client ;
- on **n'invente pas** de NIU ;
- on **n'utilise pas le NIU de l'entreprise** comme NIU acheteur ;
- le NIU de l'entreprise reste uniquement dans `taxpayer_niu` (le vendeur).

Le mot « automatique » signifie ici : **le code détecte automatiquement qu'un
client est un client comptoir et adapte le payload SFEC**, sans modifier les
données existantes.

> Cette proposition ne corrige pas `FCDFG016` : le client `Bague's en or`
> n'est pas un client comptoir. Son NIU actuel `546789` reste invalide et doit
> être corrigé côté Sage, ou la facture doit être reclassée en particulier si
> c'est une vente B2C.

---

## 2. Règles cibles

| Situation | `recipient_type` | `recipient_niu` | Résultat |
|---|---|---|---|
| Client comptoir, NIU vide | `individual` | absent | accepté |
| Particulier / étranger, NIU vide | `individual` / `foreign` | absent | accepté |
| Entreprise avec NIU valide 16/17 | `business` | envoyé | accepté |
| Gouvernement avec NIU valide 16/17 | `government` | envoyé | accepté |
| Entreprise / gouvernement sans NIU | `business` / `government` | absent | rejet `422` |
| NIU présent mais invalide | type inchangé | rejet | pas de remplacement automatique |
| NIU entreprise | jamais utilisé comme NIU acheteur | — | uniquement `taxpayer_niu` |

Règles complémentaires :

- normaliser le NIU client en majuscules et supprimer les espaces ;
- un NIU valide doit contenir **16 ou 17 caractères alphanumériques** ;
- si un NIU client est présent mais invalide, on **rejette** avec un message
  clair ; on ne le remplace pas silencieusement ;
- si le NIU est vide et que le client n'est pas comptoir, on conserve l'erreur
  `recipient_niu requis pour recipient_type=...` ;
- aucun NIU n'est écrit en base.

---

## 3. Détection automatique du client comptoir

### 3.1 Priorité de détection

1. **Code client** dans une liste configurable :
   - par défaut : `["CLI-CPT"]` ;
   - clé de configuration : `sfec.counter_customer_codes`.
2. **Nom du client** contenant un mot-clé configurable :
   - par défaut : `["COMPTOIR", "COMPTANT"]` ;
   - clé de configuration : `sfec.counter_name_keywords`.
3. **Type fiscal explicitement `individual` / `particulier`** déjà présent
   dans la facture.

Le code est prioritaire sur le nom. Le nom permet de reconnaître les comptes
comptoir qui n'ont pas exactement le code `CLI-CPT`, sans toucher à Sage.

### 3.2 Exemple de helper

```python
COUNTER_CODES = {"CLI-CPT"}
COUNTER_NAME_KEYWORDS = ("COMPTOIR", "COMPTANT")


def _is_counter_customer(invoice, cfg=None):
    cfg = cfg or {}
    codes = {str(c).strip().upper()
             for c in (cfg.get("counter_customer_codes") or COUNTER_CODES)}
    keywords = tuple(
        str(k).strip().upper()
        for k in (cfg.get("counter_name_keywords") or COUNTER_NAME_KEYWORDS)
    )

    code = str(
        invoice.get("tiers_code")
        or invoice.get("code_tiers")
        or ""
    ).strip().upper()
    if code and code in codes:
        return True

    name = str(
        invoice.get("nom_tiers")
        or invoice.get("tiers_nom")
        or ""
    ).strip().upper()
    return bool(name) and any(k in name for k in keywords)
```

Ce helper est **en lecture seule** : il ne fait aucun `UPDATE`, aucun `INSERT`,
aucune migration.

---

## 4. Modifications prévues

### 4.1 `app/integration/sfec/endpoints.py` — payload

Dans `db_invoice_to_sfec()` :

- récupérer le code client via `tiers_code` ou `code_tiers` ;
- récupérer le nom via `nom_tiers` ou `tiers_nom` ;
- calculer `recipient_type` avec `_map_recipient_type(raw_type)` ;
- si `_is_counter_customer(invoice)` est vrai, forcer
  `recipient_type = "individual"` ;
- ne jamais affecter `company.tax_number` à `recipient_niu` ;
- n'ajouter `recipient_niu` que si le NIU client est non vide ;
- laisser `validate_sfec_payload()` refuser un NIU invalide ou un
  `business`/`government` sans NIU.

Pseudo-code :

```python
raw_niu = _safe_str(invoice.get("recipient_niu")).upper().replace(" ", "")
recipient_type = _map_recipient_type(invoice.get("recipient_type_raw"))

if _is_counter_customer(invoice, sfec_cfg):
    recipient_type = "individual"

sfec_req["recipient_type"] = recipient_type
if raw_niu:
    sfec_req["recipient_niu"] = raw_niu
# Pas de fallback company.tax_number ici.
```

`taxpayer_niu` continue d'être renseigné séparément avec le NIU de
l'entreprise.

### 4.2 `app/integration/sfec/endpoints.py` — factures locales

Dans `sqlite_invoice_to_sfec()` :

- transmettre `tiers_code` en plus de `tiers_type`, `tiers_nom`,
  `tiers_niu` ;
- cela permet de reconnaître `CLI-CPT` même si l'ancienne facture locale a
  encore `tiers_type = "business"`.

### 4.3 `app/integration/sage/database.py` — alias lecture seule

`fetch_sales_invoices()` expose déjà `code_tiers`, mais le miroir local et
certains appelants utilisent `tiers_code`.

Ajouter, après la construction de chaque `inv`, un alias en lecture seule :

```python
inv["tiers_code"] = inv.get("code_tiers", "")
```

Aucune écriture SQL, aucun changement de schéma. Cela évite que le code de
détection du comptoir dépende du chemin d'appel.

### 4.4 POS — `app/domain/pos.py` et `app/web/routes/pos.py`

- Dans `_resolve_ticket_tiers()`, si le code ou le nom correspond à un client
  comptoir, forcer `tiers_type = "individual"`.
- Dans la route POS qui construit `pos_clients_data`, exposer
  `type_raw = "individual"` pour `CLI-CPT` **calculé à la volée**, sans écrire
  dans `contacts`.
- Le front POS continue d'envoyer `tiers_type`, mais le backend reste la source
  de vérité.

### 4.5 Formulaire de facturation — `app/web/templates/billing/form.html`

Dans `fillContact()` :

- si le code contact est `CLI-CPT` ou si le nom contient `COMPTOIR` /
  `COMPTANT`, sélectionner `individual` ;
- ne pas demander de NIU dans ce cas ;
- ne pas modifier le contact en base.

Le formulaire possède déjà les valeurs `business`, `individual`,
`government`, `foreign` ; aucune nouvelle option n'est nécessaire.

### 4.6 Configuration — `config.example.json` / `app/config/manager.py`

Ajouter deux clés optionnelles dans la section `sfec` :

```json
"counter_customer_codes": ["CLI-CPT"],
"counter_name_keywords": ["COMPTOIR", "COMPTANT"]
```

Si les clés sont absentes, le code utilise les valeurs par défaut ci-dessus.
Ce sont des paramètres de comportement, pas des données client.

---

## 5. Aucune modification de données

Cette proposition ne doit contenir :

- aucun `UPDATE contacts` ;
- aucun `INSERT` de contact ;
- aucune migration de données ;
- aucun backfill ;
- aucune écriture dans Sage ;
- aucune correction automatique des NIU existants.

Le contact `CLI-CPT` peut rester exactement comme aujourd'hui :

```text
type      = client
type_raw  = vide
niu       = vide
```

Le comportement `individual` est calculé au moment de construire le payload
SFEC, puis oublié. La base reste la source de vérité.

---

## 6. Tests

Créer `tests/test_sfec_niu.py` avec les cas suivants :

1. `CLI-CPT` sans NIU → `recipient_type = "individual"`, pas de
   `recipient_niu`.
2. Client dont le nom contient `COMPTOIR` sans NIU → `individual`.
3. Client dont le nom contient `COMPTANT` sans NIU → `individual`.
4. Client B2B `Bague's en or` avec `NIU = "546789"` → rejet `422`, pas de
   remplacement.
5. Client B2B sans NIU → rejet `422`.
6. Client B2B avec NIU valide 16/17 → NIU transmis.
7. NIU entreprise → présent dans `taxpayer_niu`, jamais dans
   `recipient_niu`.
8. Facture Sage avec `code_tiers = "CLI-CPT"` → `individual`.
9. Facture locale avec `tiers_code = "CLI-CPT"` et
   `tiers_type = "business"` → `individual` (surcharge au payload).
10. Facture locale `individual` sans NIU → acceptée.
11. Vérifier que la table `contacts` est identique avant et après la
    construction du payload (aucune écriture).
12. Vérifier que les clés de configuration `counter_customer_codes` et
    `counter_name_keywords` surchargent bien les valeurs par défaut.

Tests existants à relancer :

```bash
.venv/bin/python -m pytest tests/test_sfec_payload_rules.py
.venv/bin/python -m pytest tests/test_sfec_certify_number.py
.venv/bin/python -m pytest tests/test_sfec_already_certified.py
.venv/bin/python -m pytest tests/test_sfec_anomalies.py
.venv/bin/python -m pytest tests/test_sfec_niu.py
```

---

## 7. Critères d'acceptation

- Une vente POS au comptoir sans NIU passe la validation locale et peut être
  certifiée.
- Une facture locale `CLI-CPT` sans NIU passe la certification.
- Une facture Sage dont le client est `CLI-CPT` passe sans NIU.
- Un client B2B sans NIU reste bloqué avec un message explicite.
- Un NIU B2B invalide reste bloqué et n'est jamais remplacé.
- Le NIU de l'entreprise n'apparaît jamais dans `recipient_niu`.
- Aucune ligne de `contacts` ni de Sage n'est modifiée.
- Aucun code de taxe n'est lu, mappé ou supposé.
- `FCDFG016` reste bloquée tant que le NIU de `Bague's en or` n'est pas
  corrigé ou que la facture n'est pas reclassée en `individual`.

---

## 8. Hors périmètre

- NIU par défaut de l'entreprise ;
- NIU fictif ou NIU généré ;
- mapping des codes de taxe Sage (`C20`, `C18`, `C009`, `C10`, etc.) ;
- centime additionnel et taxes supplémentaires ;
- garde-fou d'auto-certification ;
- correction des données `FCDFG016` / `FCDFG015` ;
- écriture du centime dans Sage.

Un mapping des codes de taxe pourra être étudié plus tard, car la base Sage du
client peut différer de la base locale. Ce n'est pas le sujet de cette
proposition.

---

## 9. Points à confirmer après implémentation

1. La liste des codes comptoir à reconnaître au-delà de `CLI-CPT`.
2. L'acceptation des mots-clés `COMPTOIR` / `COMPTANT` pour la détection par
   nom. Si un vrai client B2B peut contenir ces mots, il faudra se limiter à
   une liste de codes.
3. La confirmation que la production SFEC accepte bien
   `recipient_type = "individual"` sans `recipient_niu`. Le sandbox et la
   validation locale l'acceptent ; si la production refuse, il faudra une
   décision séparée (option 2 : NIU par défaut dédié, jamais le NIU de
   l'entreprise).

---

## 10. Validation après implémentation

1. Lancer les tests unitaires ci-dessus.
2. Faire une vente POS « Client comptoir » sans NIU.
3. Vérifier dans le log le payload :
   `recipient_type=individual`, absence de `recipient_niu`.
4. Créer une facture de facturation avec `CLI-CPT`, vérifier l'absence de
   blocage NIU.
5. Certifier une facture Sage `CLI-CPT` sur le sandbox.
6. Vérifier qu'une facture B2B sans NIU est toujours refusée.
7. Vérifier que la table `contacts` n'a pas changé.
