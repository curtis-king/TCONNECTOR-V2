# Rapport d'écart et plan de correction — Push Sage et désignations/articles Sage

**Date :** 2026-10-23
**Branche :** `fix/alu-congo`
**Périmètre :** écriture des factures locales vers Sage 100, pull bidirectionnel, référentiel articles, saisie des lignes de facture web et POS.
**Base Sage observée :** `BIJOU` (`10.42.0.110,1433`) en **lecture seule**.
**Base locale observée :** `connecteur-python/data/tconnector.db` en **lecture seule**.

> Ce document est un **rapport d'écart et un plan de correction**. Aucun code ni aucune donnée
> n'a été modifié. Les opérations sur `BIJOU` restent soumises à un accord explicite.

---

## 1. Résumé exécutif

Deux familles de défauts ont été confirmées :

1. **Le writer Sage insère l'en-tête `F_DOCENTETE` mais échoue sur les lignes `F_DOCLIGNE`.**
   La colonne `PF_Num` est `NOT NULL` dans `BIJOU` et le writer ne l'alimente pas. La connexion
   étant en `autocommit=True`, l'en-tête reste orphelin. Le pull suivant écrase ensuite les
   totaux locaux de la facture, ce qui produit `INV-000004` et `FA000003` dans un état incohérent.

2. **Les lignes de facture web sont saisies librement, sans lien obligatoire avec un article Sage.**
   Le formulaire de facturation permet de taper une désignation, un prix et une TVA arbitraires.
   La liste `products-list` est vide et le `product_id` n'est pas envoyé. Le POS conserve aussi
   une fonction « ligne libre ». Le référentiel local mélange 69 articles synchronisés depuis Sage
   et 60 articles de démonstration, et la synchronisation articles ne récupère pas correctement
   la TVA, l'unité, le type ni le statut actif dans `BIJOU`.

La correction doit donc être faite en deux blocs indissociables :

- **fiabiliser l'écriture Sage** (transaction, `PF_Num`, numérotation `DL_Ligne`, calcul de
  l'en-tête depuis les lignes, idempotence, pas d'écrasement des factures locales au pull) ;
- **faire de Sage la source de vérité des articles et désignations** (catalogue miroir,
  résolution serveur obligatoire, champs verrouillés, suppression de la saisie libre).

---

## 2. Cas soulevés et constats vérifiés

### 2.1 `INV-000004` — en-tête Sage orphelin

**Preuves lues dans Sage :**

```text
F_DOCENTETE :
  DO_Domaine = 0, DO_Type = 6, DO_Piece = 'INV-000004'
  DO_TotalHT  = 0
  DO_TotalTTC = 347 782,50
  DO_Taxe1    = 52 650
  DO_CodeTaxe1 = 'C18'
  DO_NetAPayer = 347 782,50
  DO_Statut = 2, DO_Valide = 1
F_DOCLIGNE :
  aucune ligne
```

**Preuves locales :**

```text
invoice_lines :
  1 ligne, 13 × 22 500
  montant_ht  = 292 500
  montant_tva = 52 650
  montant_ttc = 345 150
  additional_cent_tax attendu = 2 632,50
invoice (après pull) :
  montant_ht  = 0
  montant_tva = 347 782,50
  montant_ttc = 347 782,50
  sfec_statut = 'CERTIFIE'
  synced_sage = 1
```

**Erreur d'écriture :**

```text
sync_log 1942 :
Impossible d'insérer la valeur NULL dans la colonne 'PF_Num',
table 'BIJOU.dbo.F_DOCLIGNE'.
```

**Cause racine :**

- `app/integration/sage/writer.py:189-207` insère dans `F_DOCLIGNE` sans `PF_Num`.
- `PF_Num` est `NOT NULL` dans `BIJOU` ; les lignes existantes utilisent `PF_Num=''`.
- La connexion est ouverte en `autocommit=True` (`app/integration/sage/database.py:78-80`).
  L'INSERT d'en-tête est donc validé avant l'échec des lignes : la pièce reste orpheline.
- Le writer recopie `invoice.get("montant_ht")` (`writer.py:179`) au lieu de recalculer
  l'en-tête depuis les lignes. L'état actuel prouve que cette valeur était `0` au moment
  du push alors que la ligne locale vaut `292 500`.
- Le pull écrase tous les totaux locaux, y compris pour `source='web'`
  (`app/sync/bidirectional.py:421-450`).

### 2.2 `FA000003` — même signature

```text
Sage :
  DO_TotalHT  = 0
  DO_TotalTTC = 59 450
  DO_Taxe1    = 9 000
  DO_CodeTaxe1 = 'C18'
  0 ligne F_DOCLIGNE
Local :
  ligne 10 × 5 000 : HT 50 000, TVA 9 000, TTC 59 000
  additional_cent_tax attendu = 450
  montant_ht = 0, montant_tva = montant_ttc = 59 450
  sfec_statut = 'A_SURVEILLER'
```

Même mécanisme : en-tête écrit, ligne refusée, puis pull qui écrase les totaux.

### 2.3 Saisie manuelle des désignations et articles

**Formulaire de facturation :**

- `app/web/routes/billing.py:227` charge `pos_engine.list_products(limit=200)`.
- `app/web/templates/billing/form.html:283` expose `products` au JavaScript.
- `app/web/templates/billing/form.html:310` affiche un champ texte libre `l_design_<n>` avec
  `list="products-list"`.
- `app/web/templates/billing/form.html:384` déclare `<datalist id="products-list"></datalist>` **vide**.
- `app/web/templates/billing/form.html:311` envoie seulement `l_code_article_<n>` et
  `l_famille_<n>`, jamais `product_id`, `unite` ni `type_article`.
- `app/web/templates/billing/form.html:369` construit la ligne avec la désignation, le prix
  et la TVA saisis par l'utilisateur.

**Validation serveur :**

- `app/domain/invoices.py:75-108` vérifie seulement : désignation non vide, quantité > 0,
  prix >= 0, remise bornée, TVA dans `(0, 5, 18)`.
- Aucune requête `products` n'est faite.
- `_save_lines()` (`app/domain/invoices.py:455-505`) accepte `product_id` mais ne l'exige pas
  et ne vérifie pas que l'article est actif ou synchronisé depuis Sage.

**POS :**

- `app/web/templates/pos/index.html:214-221` expose `addPosLineManual()` qui crée une ligne
  avec `designation`, `prix_unitaire`, `product_id=null`.
- `app/domain/pos.py:41-102` valide les lignes du ticket sans exiger d'article Sage.

### 2.4 Référentiel articles : Sage n'est pas réellement la source

`fetch_articles()` (`app/integration/sage/database.py:1587-1675`) détecte les colonnes
disponibles puis applique des valeurs de repli :

- `app/integration/sage/database.py:1606-1613` : `_col()` retourne une constante si la colonne
  est absente.
- `app/integration/sage/database.py:1622` : `AR_UniteVen` est converti en texte brut.
- `app/integration/sage/database.py:1623` : `AR_Inactif` est utilisé, avec repli `0`.
- `app/integration/sage/database.py:1625` : `AR_CodeTVA` / `FA_CodeTVA` sont utilisés,
  avec repli `'18'`.

Or, dans `BIJOU` :

```text
F_ARTICLE ne contient ni AR_CodeTVA ni AR_Inactif.
F_ARTCOMPTA contient la TVA de vente :
  ACP_Type = 0 (vente), ACP_Champ = 1 (tarif par défaut)
  60 articles -> C20
   1 article  -> C00
   8 articles de service n'ont pas de ligne F_ARTCOMPTA de vente ;
     leur TVA vient de F_FAMCOMPTA (C20 pour PRESTFORM, PRESTSAV, PRESTMAINT).
F_TAXE :
  C20 = 20 %, C00 = 0 %, C18 = 18 %, C05 = 5 %
P_UNITE :
  cbIndice 1 = Pièce, 4 = Unité
```

Résultat consolidé après application de `F_ARTCOMPTA` puis `F_FAMCOMPTA` :
**68 articles à C20 (20 %) et 1 article à C00 (0 %)** — et non 69 articles à 18 %.

Conséquences vérifiées :

- les 69 articles Sage importés localement ont tous `tva_code='18'`, alors que la majorité
  devrait être à `20 %` ou `0 %` dans `BIJOU` ;
- `unite` vaut `'1'` ou `'4'` au lieu de `Pièce` / `Unité` ;
- `AR_Inactif` absent => tous les articles sont considérés actifs ;
- `article_type` est bien retourné par `fetch_articles()` mais n'est **pas stocké** par
  `_upsert_products()` (`app/sync/article_sync.py:8-99`) ;
- `AR_PrixTTC` est ignoré : `fetch_articles()` prend `AR_PrixVen` sans normaliser le HT/ TTC
  (`database.py:1620`). Un article de `BIJOU` (`ZFIDELITE`) est marqué `AR_PrixTTC=1`.

Le référentiel local contient :

```text
129 produits :
  69 synchronisés Sage (synced_sage=1)
  60 articles de démonstration (synced_sage=0)
```

`list_products()` (`app/domain/pos.py:1029-1049`), `search_products()`
(`app/domain/pos.py:702-715`), `find_product_by_barcode()` (`pos.py:718-722`) et
`top_selling_products()` (`pos.py:725-...`) ne filtrent pas `synced_sage`.
Les articles de démonstration restent donc sélectionnables.

`main.py:36-44` synchronise les articles au démarrage, mais `main.py:45-54` peut ensuite
semer 60 articles de démonstration si la base est vide. Ce fallback hors-ligne est incompatible
avec la règle « les articles proviennent de Sage ».

Enfin, `sync_all()` synchronise les articles (`app/sync/engine.py:572-575`), mais
`sync_incremental()` ne le fait pas. Le catalogue peut donc rester obsolète entre deux
redémarrages ou deux synchronisations manuelles.

### 2.5 Pull bidirectionnel : écrasement des factures locales

`pull_from_sage()` (`app/sync/bidirectional.py:368-399`) retrouve une facture uniquement par
`numero`. Si elle existe, `_update_from_sage()` (`bidirectional.py:402-450`) met à jour :

- `montant_ht`, `montant_tva`, `montant_ttc`, `montant_restant` ;
- les taxes, remises, centimes et timbre ;
- `type_doc`, les références Sage, `synced_sage=1`.

Aucune distinction n'est faite selon `source`. Une facture `web` ou `pos` est donc écrasée par
l'en-tête Sage, même si ses lignes locales sont la source de vérité. C'est exactement ce qui a
corrompu `INV-000004` et `FA000003`.

---

## 3. Modèle cible

### 3.1 Source de vérité

```text
Sage F_ARTICLE / F_ARTCOMPTA / F_TAXE / P_UNITE
        │
        │  synchronisation lecture seule
        ▼
products (miroir local, synced_sage=1)
        │
        │  résolution serveur obligatoire à la création de la ligne
        ▼
invoice_lines (snapshot figé : ref, désignation, prix, TVA, unité, type)
```

Règles :

- **Sage est la source de vérité** pour la référence, la désignation, le prix de vente HT,
  la TVA, l'unité, la famille et le type d'article.
- **Le miroir local `products`** est utilisé pour la recherche et la performance ; il ne doit
  contenir que des articles issus de Sage pour la facturation.
- **La ligne de facture conserve un snapshot** des valeurs au moment de la création. Une
  modification ultérieure de l'article dans Sage ne réécrit jamais une facture émise.
- **Le client ne peut pas imposer** une désignation, un prix ou une TVA : le serveur résout
  l'article et écrase les valeurs envoyées.
- **Les articles non synchronisés, inactifs ou à TVA inconnue ne sont pas sélectionnables.**
- **La quantité et la remise de ligne restent modifiables.** Une remise commerciale doit
  passer par `remise_pct`, pas par une modification du prix unitaire.

### 3.2 Champs obligatoires d'une ligne de facture locale

À la création/mise à jour d'une facture `web` ou `pos` :

```text
product_id        : obligatoire, article actif et synced_sage=1
code_article      : copié depuis products.ref / sage_ar_ref (référence Sage)
designation       : copiée depuis products.designation
prix_unitaire     : copié depuis products.prix_vente (HT)
taux_tva          : copié depuis products.tva_code (taux numérique)
famille           : copiée depuis products.famille
unite             : copiée depuis products.unite
type_article      : copié depuis products.article_type
classification_code : famille Sage ou code de classification
```

Les champs `quantite`, `remise_pct`, `discount_type` restent pilotés par l'utilisateur.

### 3.3 Factures historiques

- Les factures déjà certifiées ou déjà écrites dans Sage sont **immuables** : on ne remappe pas
  leurs lignes.
- Les factures locales non certifiées qui contiennent des lignes libres historiques doivent
  être **remappées** avant certification, ou rester bloquées.
- Aucun contournement de type « ligne libre » ne doit être ajouté en production.

---

## 4. Plan de correction

### Lot A — Writer Sage fiable

**Fichier principal :** `app/integration/sage/writer.py`
**Fichiers annexes :** `app/integration/sage/database.py`, `app/sync/bidirectional.py`

#### A1. Transaction réelle autour de l'en-tête et des lignes

Aujourd'hui `get_cursor()` commit à la sortie et la connexion est en `autocommit=True`.
Il faut un curseur transactionnel dédié, ou basculer temporairement la connexion en
`autocommit=False` avec `commit()` / `rollback()` et restauration avant remise au pool.

**Bloc à remplacer** — `app/integration/sage/database.py:449-491` :

```python
@contextmanager
def get_cursor():
    ...
    yield cursor
    conn.commit()
```

**Bloc corrigé (principe) :**

```python
@contextmanager
def get_cursor(transaction=False):
    conn = _checkout(...)
    previous_autocommit = conn.autocommit
    if transaction:
        conn.autocommit = False
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if transaction:
            conn.autocommit = previous_autocommit
        _checkin(conn)
```

Le writer doit utiliser ce mode pour l'ensemble `F_DOCENTETE` + `F_DOCLIGNE`.
En cas d'échec, **aucun en-tête orphelin** ne doit rester.

#### A2. Alimenter `PF_Num` et normaliser `DL_Ligne`

**Bloc à remplacer** — `writer.py:187-207` :

```python
cur.execute("""
    INSERT INTO F_DOCLIGNE (
        DO_Domaine, DO_Type, DO_Piece, DL_Ligne,
        DL_Design, DL_Qte, DL_PrixUnitaire, DL_MontantHT,
        DL_CodeTaxe1, DL_Taxe1, DL_MontantTTC, CO_No, AR_Ref
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    dc["vente_domaine"], doc_type, numero,
    ligne.get("numero_ligne", 1),
    ...
    ligne.get("code_compte", ""),
    ligne.get("code_article", "")
))
```

**Bloc corrigé (principe) :**

```python
# DL_Ligne Sage : 1000, 2000, 3000... (pas 1, 2, 3)
dl_ligne = (index + 1) * 1000

# PF_Num est NOT NULL dans BIJOU ; les lignes existantes utilisent ''
pf_num = ""

# CO_No est un int : ne jamais envoyer '' (erreur de conversion)
co_no = None
if str(ligne.get("code_compte") or "").strip().isdigit():
    co_no = int(str(ligne["code_compte"]).strip())

cur.execute("""
    INSERT INTO F_DOCLIGNE (
        DO_Domaine, DO_Type, DO_Piece, DL_Ligne, PF_Num,
        DL_Design, DL_Qte, DL_PrixUnitaire, DL_MontantHT,
        DL_CodeTaxe1, DL_Taxe1, DL_MontantTTC, CO_No, AR_Ref
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    dc["vente_domaine"], doc_type, numero, dl_ligne, pf_num,
    ligne.get("designation", ""),
    sign * ligne.get("quantite", 1),
    ligne.get("prix_unitaire", 0),
    sign * ligne.get("montant_ht", 0),
    _code_taxe(ligne.get("taux_tva", 18)),
    ligne.get("taux_tva", 18),
    sign * ligne.get("montant_ttc", 0),
    co_no,
    ligne.get("code_article", "")
))
```

**Robustesse multi-schémas :** construire l'INSERT à partir des colonnes réellement présentes
dans `F_DOCLIGNE` (`INFORMATION_SCHEMA`). Si `PF_Num` existe, l'inclure avec `''` ; si
`CO_No` existe, envoyer `None` ou un entier, jamais `''`.

#### A3. Calculer l'en-tête depuis les lignes, pas depuis l'en-tête local

**Bloc à remplacer** — `writer.py:163-185` :

```python
taux_entete = (invoice.get("lignes") or [{}])[0].get("taux_tva", 18)
code_entete = _code_taxe(taux_entete)
...
sign * invoice.get("montant_ht", 0),
code_entete,
sign * invoice.get("montant_tva", 0),
sign * invoice.get("montant_ttc", 0),
sign * invoice.get("montant_ttc", 0),
```

**Bloc corrigé (principe) :**

```python
def _document_totals(invoice, sign):
    lignes = invoice.get("lignes") or []
    ht = round(sum(float(l.get("montant_ht") or 0) for l in lignes), 2)
    tva = round(sum(float(l.get("montant_tva") or 0) for l in lignes), 2)

    # Le TTC de ligne n'inclut pas les centimes/timbre : ils sont portés par
    # la facture et déjà calculés par recalc_invoice_totals().
    ttc_lignes = round(sum(float(l.get("montant_ttc") or 0) for l in lignes), 2)
    centimes = float(invoice.get("additional_cent_tax") or 0)
    timbre = float(invoice.get("electronic_stamp_duty") or 0)
    ttc = round(ttc_lignes + centimes + timbre, 2)

    codes = {_code_taxe(l.get("taux_tva", 18)) for l in lignes}
    code_entete = codes.pop() if len(codes) == 1 else ""
    return sign * ht, code_entete, sign * tva, sign * ttc
```

Points importants :

- `DO_TotalHT` doit être la somme des lignes, jamais la valeur locale potentiellement corrompue.
- `DO_Taxe1` doit être la somme des TVA de lignes.
- `DO_TotalTTC` doit inclure les centimes et le timbre déjà calculés localement.
- Si les lignes ont plusieurs taux, ne pas prétendre à un code unique : soit ventiler, soit
  bloquer le push avec un message explicite. Ce point est à traiter séparément si nécessaire.
- Appeler `recalc_invoice_totals()` avant tout push d'une facture locale, afin que
  `additional_cent_tax` et `electronic_stamp_duty` soient à jour.

#### A4. Idempotence : compléter une pièce existante, ne jamais la recréer

Avant l'INSERT d'en-tête :

```sql
SELECT 1 FROM F_DOCENTETE
WHERE DO_Domaine = ? AND DO_Type = ? AND DO_Piece = ?
```

Comportement attendu :

| État Sage | Action |
|---|---|
| en-tête absent | créer en-tête + lignes dans la transaction |
| en-tête présent, 0 ligne | **compléter** la pièce existante (cas `INV-000004` / `FA000003`) |
| en-tête présent, lignes présentes | ne rien réinsérer ; vérifier les totaux et marquer local `synced_sage=1` |
| en-tête présent, lignes incohérentes | ne pas supprimer ; classer `A_SURVEILLER` et exiger une intervention |

Le writer ne doit **jamais** supprimer ou recréer une pièce certifiée.

#### A5. Nettoyage des orphelins

Avec la transaction, le cas ne doit plus se produire. Pour les pièces déjà orphelines :

- ne pas supprimer l'en-tête ;
- insérer les lignes manquantes ;
- recalculer l'en-tête ;
- vérifier le nombre de lignes et les totaux ;
- consigner l'opération dans `sync_log`.

Un script de réparation idempotent est décrit au lot E.

---

### Lot B — Pull bidirectionnel non destructif

**Fichier :** `app/sync/bidirectional.py`

#### B1. Ne pas écraser les totaux des factures `web` / `pos`

**Bloc à remplacer** — `bidirectional.py:421-450` :

```python
montant_ht = sage_inv.get("montant_ht", 0) or 0
...
cur.execute("""
    UPDATE invoices SET
        montant_ht = ?, montant_tva = ?, montant_ttc = ?,
        ...
""")
```

**Bloc corrigé (principe) :**

```python
with get_cursor() as cur:
    cur.execute("""
        SELECT source, montant_ht, montant_tva, montant_ttc,
               additional_cent_tax, electronic_stamp_duty
        FROM invoices WHERE id = ?
    """, (invoice_id,))
    local = cur.fetchone()

source = (local["source"] or "").lower() if local else ""

if source in ("web", "pos"):
    # Les lignes locales sont la source de vérité. On ne met à jour que
    # le lien Sage et les champs SFEC.
    _check_sage_totals_against_local(invoice_id, sage_inv, local)

    cur.execute("""
        UPDATE invoices SET
            sage_domaine = ?, sage_type = ?, sage_piece = ?,
            synced_sage = 1, updated_at = datetime('now')
        WHERE id = ?
    """, (...))
else:
    # Comportement existant pour les factures importées de Sage.
    cur.execute(""" ... mise à jour complète ... """)
```

`_check_sage_totals_against_local()` :

- compare `sage_inv["montant_ttc"]` au TTC local recalculé depuis les lignes ;
- tolérance : `1,0 FCFA` (même logique que `check_sage_totals()`) ;
- en cas d'écart, **ne pas corriger silencieusement** : journaliser, appeler
  `mark_to_monitor()` et conserver les totaux locaux ;
- ne jamais remettre `additional_cent_tax` à `0` pour une facture `web`/`pos`.

#### B2. Garde sur les factures importées de Sage

Pour `source='sage'`, conserver la mise à jour complète. Mais :

- ne pas écraser une ligne locale `web`/`pos` ;
- si `source` est vide ou inconnue, traiter comme `sage` uniquement si `sage_piece` est déjà
  positionné et qu'il n'existe pas de lignes locales avec `product_id`.

#### B3. Cas des factures sans lignes

Le pull ne doit pas considérer une facture locale `web`/`pos` comme valide si ses lignes sont
absentes. Si le nombre de lignes locales est `0`, classer `A_SURVEILLER` et ne pas inventer de
totaux.

---

### Lot C — Référentiel articles Sage

**Fichiers :** `app/integration/sage/database.py`, `app/sync/article_sync.py`,
`app/storage/db.py`, `app/domain/pos.py`, `app/web/routes/pos.py`, `main.py`.

#### C1. Colonnes locales à ajouter à `products`

Migration `_migrate()` (`app/storage/db.py:454` et suivants) :

```text
sage_taxe_code   TEXT DEFAULT ''   -- C18, C20, C00, C05...
article_type     TEXT DEFAULT 'product'  -- product / service
sage_synced_at   TEXT DEFAULT ''   -- horodatage de la dernière synchro
sync_status      TEXT DEFAULT ''   -- ok / incomplete / inactive
```

On conserve `tva_code` comme **taux numérique compatible** avec le code existant
(`'18'`, `'5'`, `'0'`, et si nécessaire `'20'` pour les bases de test).
`sage_taxe_code` conserve le code Sage pour l'écriture et l'audit.

#### C2. Corriger `fetch_articles()`

**Bloc à remplacer** — `database.py:1606-1625` :

```python
unite_s = _col(("AR_UNITEVEN", ...), "'U'", cast=True)
inactif_s = _col(("AR_INACTIF",), "0")
tva_s = _col(("AR_CODETVA", "FA_CODETVA"), "'18'", cast=True)
```

**Bloc corrigé (principe SQL) :**

```sql
SELECT
    a.AR_Ref,
    a.AR_Design,
    a.FA_CodeFamille,
    a.AR_Nature,
    a.AR_Type,
    a.AR_PrixVen,
    a.AR_PrixTTC,
    a.AR_CodeBarre,
    ISNULL(u.U_Intitule, '') AS unite,
    ISNULL(t.TA_Code, '')    AS sage_taxe_code,
    ISNULL(t.TA_Taux, NULL)  AS tva_rate,
    CASE
        WHEN ISNULL(a.AR_Sommeil, 0) = 1 THEN 0
        WHEN ISNULL(a.AR_Inactif, 0) = 1 THEN 0
        ELSE 1
    END AS est_actif
FROM F_ARTICLE a
LEFT JOIN P_UNITE u
       ON u.cbIndice = a.AR_UniteVen
OUTER APPLY (
    SELECT TOP 1 ac.ACP_ComptaCPT_Taxe1 AS taxe_code
    FROM F_ARTCOMPTA ac
    WHERE ac.AR_Ref = a.AR_Ref
      AND ac.ACP_Type = 0          -- vente
    ORDER BY CASE WHEN ac.ACP_Champ = 1 THEN 0 ELSE 1 END,
             ac.ACP_Champ
) art_tax
OUTER APPLY (
    SELECT TOP 1 fc.FCP_ComptaCPT_Taxe1 AS taxe_code
    FROM F_FAMCOMPTA fc
    WHERE fc.FA_CodeFamille = a.FA_CodeFamille
      AND fc.FCP_Type = 0          -- vente
    ORDER BY CASE WHEN fc.FCP_Champ = 1 THEN 0 ELSE 1 END,
             fc.FCP_Champ
) fam_tax
LEFT JOIN F_TAXE t
       ON t.TA_Code = COALESCE(art_tax.taxe_code, fam_tax.taxe_code)
```

Le fallback famille est indispensable dans `BIJOU` : 8 articles de service n'ont pas de ligne
`F_ARTCOMPTA` de vente et leur TVA `C20` vient de `F_FAMCOMPTA`.

Règles :

- **Ne plus inventer `18 %`** quand la TVA Sage est absente. Retourner `tva_code=''`,
  `sync_status='incomplete'`, et rendre l'article non sélectionnable.
- Si `AR_PrixTTC=1`, normaliser `prix_vente` en HT :
  `prix_vente = AR_PrixVen / (1 + tva_rate / 100)`.
- Joindre `P_UNITE` pour obtenir `Pièce`, `Unité`, etc. Conserver éventuellement
  `AR_UniteVen` brut dans un champ d'audit.
- Statut actif : utiliser les colonnes réellement présentes. `AR_Sommeil` est le candidat
  le plus probable dans `BIJOU` ; `AR_Inactif` n'existe pas. Ne pas utiliser `AR_Fictif` ou
  `AR_Exclure` sans validation métier.
- Type d'article : stocker `AR_Type` et/ou `AR_Nature` brut, puis appliquer une table de
  correspondance explicite. Dans `BIJOU`, `AR_Type=2` et `AR_Type=3` couvrent les articles
  de service (coût SAV, formation, location) ; `AR_Nature=4` couvre les remises/port.
  **Cette correspondance doit être confirmée avec la nomenclature Sage avant implémentation.**
- Conserver `_merge_article_stock()` pour le stock.

#### C3. Stocker les nouveaux champs dans `_upsert_products()`

**Bloc à remplacer** — `app/sync/article_sync.py:45-46` et `:78-95` :

```python
normed.append((ref, barcode, designation, famille, nature, prix_vente,
               prix_achat, tva_code, unite, stock, est_actif))
```

**Bloc corrigé (principe) :**

```python
normed.append((
    ref, barcode, designation, famille, nature, prix_vente,
    prix_achat, tva_code, sage_taxe_code, unite, stock, est_actif,
    article_type, sync_status, now_iso
))
```

Puis :

```sql
UPDATE products SET
    barcode = ?, designation = ?, famille = ?,
    nature = ?, prix_vente = ?, prix_achat = ?,
    tva_code = ?, sage_taxe_code = ?, unite = ?, stock_reel = ?,
    est_actif = ?, article_type = ?, sync_status = ?,
    sage_synced_at = ?, synced_sage = 1, sage_ar_ref = ?,
    updated_at = datetime('now')
WHERE id = ?
```

et l'INSERT correspondant avec les mêmes colonnes.

Règles :

- `synced_sage=1` uniquement si `ref`, `designation`, `tva_code` valides ;
- `sync_status='incomplete'` si la TVA ou l'unité est introuvable ;
- ne jamais mettre à jour une ligne de facture existante : seul le catalogue change.

#### C4. Synchronisation périodique

- Au démarrage : conserver `main.py:36-44`.
- Ajouter une synchronisation périodique dans `sync_incremental()` ou dans la boucle de
  polling, avec un intervalle configurable (proposition : 60 minutes).
- Exposer un état (`last_sync`, `total`, `incomplete`) via l'API ou les logs.
- En cas d'échec de synchronisation, **ne pas semer** d'articles de démonstration :
  l'application doit afficher « Catalogue Sage indisponible » et bloquer la création de
  factures, pas fabriquer un catalogue local.

`main.py:45-54` doit être modifié :

```python
# Avant
if not nb_articles:
    seed_products()
```

```python
# Après
if not nb_articles and os.environ.get("TCONNECTOR_ALLOW_SEED_PRODUCTS") == "1":
    seed_products()
```

#### C5. Filtrer le catalogue utilisable

Ajouter un filtre commun :

```python
SAGE_SELLABLE_WHERE = """
    est_actif = 1
    AND synced_sage = 1
    AND sage_ar_ref <> ''
    AND tva_code <> ''
    AND sync_status <> 'incomplete'
"""
```

Appliquer à :

- `list_products()` (`app/domain/pos.py:1029`) ;
- `search_products()` (`pos.py:702`) ;
- `find_product_by_barcode()` (`pos.py:718`) ;
- `top_selling_products()` (`pos.py:725`) ;
- `GET /api/products` (`app/web/routes/pos.py:28-36`), avec `sage_only=true` par défaut ;
- `GET /api/products/barcode` (`app/web/routes/pos.py:314`).

Les articles `synced_sage=0` restent en base pour l'historique et les tests, mais ne sont
plus proposés à la vente. Le script `scripts/purge_seed_products.py` peut être exécuté après
une synchronisation réussie, en `dry-run` puis `--apply`, avec accord explicite.

#### C6. Interdire la création manuelle en production

- `POST /api/products` (`app/web/routes/pos.py:39-47`) doit retourner `403` par défaut,
  ou être conditionné à `TCONNECTOR_ALLOW_LOCAL_PRODUCTS=1` pour les tests.
- Aucun écran ne doit proposer « Nouveau produit ».
- Les articles locaux existants restent `synced_sage=0` et invisibles à la facturation.

---

### Lot D — Résolution serveur et formulaires

**Fichiers :** `app/domain/invoices.py`, `app/domain/pos.py`,
`app/web/templates/billing/form.html`, `app/web/templates/pos/index.html`,
`app/web/routes/billing.py`, `app/web/routes/pos.py`.

#### D1. Nouveau résolveur d'article

Ajouter dans `app/domain/articles.py` (ou `app/domain/pos.py` si l'on veut éviter un module) :

```python
class ArticleIntegrityError(ValueError):
    pass


def resolve_sage_article(product_id=None, code_article=None):
    """Retourne l'article Sage actif, ou leve ArticleIntegrityError."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT *
            FROM products
            WHERE est_actif = 1
              AND synced_sage = 1
              AND sage_ar_ref <> ''
              AND tva_code <> ''
              AND (
                  (? IS NOT NULL AND id = ?)
                  OR (? <> '' AND (ref = ? OR sage_ar_ref = ?))
              )
            LIMIT 1
        """, (product_id, product_id, code_article, code_article, code_article))
        row = cur.fetchone()

    if not row:
        raise ArticleIntegrityError(
            "Article Sage inconnu, inactif ou non synchronise : {}".format(
                code_article or product_id
            )
        )
    return row_to_dict(row)
```

#### D2. Validation des lignes de facture

**Bloc à remplacer** — `app/domain/invoices.py:62` et `:75-108` :

```python
_ALLOWED_TVA = (0.0, 5.0, 18.0)

def _validate_invoice_lines(lignes):
    ...
    designation = str(ligne.get("designation", "") or "").strip()
    if not designation:
        raise ...
    prix = _as_float(ligne.get("prix_unitaire", 0), "prix unitaire")
    taux = _as_float(ligne.get("taux_tva", 18), "taux TVA")
    ...
```

**Bloc corrigé (principe) :**

```python
def _validate_invoice_lines(lignes, require_sage_article=True):
    ...
    for index, ligne in enumerate(lignes, start=1):
        ...
        if require_sage_article:
            product = resolve_sage_article(
                product_id=ligne.get("product_id"),
                code_article=ligne.get("code_article"),
            )
            taux = _as_float(product["tva_code"], "taux TVA")
            if taux not in _ALLOWED_TVA:
                raise InvoiceIntegrityError(
                    "Ligne {} : taux de TVA Sage non supporte ({})".format(index, taux)
                )
            item = dict(ligne)
            item.update({
                "product_id": product["id"],
                "code_article": product["ref"],
                "designation": product["designation"],
                "prix_unitaire": float(product["prix_vente"]),
                "taux_tva": taux,
                "famille": product["famille"],
                "unite": product["unite"],
                "type_article": product.get("article_type") or "product",
                "classification_code": product["famille"],
            })
            # Les valeurs client sont ignorées : on peut les journaliser
            # si elles diffèrent, pour détecter une tentative de manipulation.
        ...
```

Appels :

- `create_invoice()` (`invoices.py:142`) : `require_sage_article=True`.
- `update_invoice()` (`invoices.py:236`) : charger `source` de la facture existante,
  puis `require_sage_article=(source in ("web", "pos"))`.
- Les imports `source='sage'` ne passent pas par cette validation.

#### D3. Formulaire de facturation

**Bloc à remplacer** — `form.html:310-315` et `:369-372` :

- champ texte `l_design_<n>` ;
- prix et TVA éditables ;
- pas de `product_id` ;
- `products` préchargé mais inutilisé ;
- datalist vide.

**Bloc corrigé (principe) :**

- Remplacer le champ désignation par un champ de recherche d'article :

```html
<label>Article Sage</label>
<input type="text" class="product-search" data-line="IDX"
       placeholder="Référence ou désignation..." autocomplete="off">
<input type="hidden" name="l_product_id_IDX">
<input type="hidden" name="l_code_article_IDX">
<div class="product-selected"></div>
```

- Afficher en lecture seule : désignation, prix HT, TVA, unité, famille, type.
- Le prix et la TVA ne sont plus des inputs éditables.
- La quantité et la remise restent éditables.
- La recherche appelle `GET /api/products?search=...&sage_only=true&limit=50`.
- `gatherData()` envoie `product_id`, `quantite`, `remise_pct`, `discount_type`.
  Le serveur ignore `designation`, `prix_unitaire` et `taux_tva` envoyés par le client.
- En édition, si une ligne n'a pas de `product_id`, tenter une résolution par
  `code_article`; si elle échoue, afficher un avertissement « ligne historique à remapper »
  et bloquer la certification.

La route `_invoice_form_page()` (`app/web/routes/billing.py:227`) ne doit plus précharger
200 articles : elle passe seulement les lignes existantes, et le formulaire fait les recherches
à la demande.

#### D4. POS

- Supprimer `addPosLineManual()` (`app/web/templates/pos/index.html:214-221`) et son bouton.
- `addToCart()` doit refuser un produit sans `product_id`.
- `_validate_ticket_lines()` (`app/domain/pos.py:41-102`) doit exiger `product_id` et résoudre
  l'article Sage, puis écraser désignation/prix/TVA comme pour la facture web.
- `search_products()` et `find_product_by_barcode()` ne retournent que des articles Sage
  vendables.
- Les tickets existants restent inchangés.

#### D5. API produits

`GET /api/products` :

```python
sage_only = request.args.get("sage_only", "true").lower() != "false"
products = pos_engine.list_products(..., sage_only=sage_only)
```

`GET /api/products/barcode` : même filtre.

`POST /api/products` : `403` sauf environnement de test explicite.

---

### Lot E — Réparation des pièces existantes

**Nouveau script :** `scripts/repair_sage_orphan_invoices.py`

#### E1. Principe

- Mode `dry-run` par défaut.
- `--apply` obligatoire pour écrire.
- Aucune suppression d'en-tête.
- Aucune recréation de pièce.
- Aucune modification de `BIJOU` sans accord explicite.
- Idempotent : relançable sans dupliquer les lignes.

#### E2. Périmètre

Pour chaque facture locale :

```text
source IN ('web','pos')
et synced_sage=1
et sage_piece non vide
et en-tête Sage existe
et nombre de lignes Sage = 0
et nombre de lignes locales > 0
```

Actions :

1. Lire les lignes locales.
2. Vérifier que chaque ligne a un `montant_ht`, `montant_tva`, `montant_ttc` cohérent.
3. Calculer l'en-tête attendu (HT, TVA, TTC, code taxe).
4. Insérer les lignes dans `F_DOCLIGNE` avec `PF_Num=''`, `DL_Ligne=1000, 2000...`,
   `CO_No` entier ou `NULL`, `AR_Ref` si disponible.
5. Mettre à jour l'en-tête existant.
6. Vérifier le nombre de lignes et les totaux.
7. Appeler `recalc_invoice_totals(invoice_id)` côté local.
8. Journaliser dans `sync_log`.

#### E3. Cas `INV-000004`

Valeurs attendues :

```text
Sage F_DOCLIGNE :
  DL_Ligne = 1000
  DL_Design = 'produit de test'
  DL_Qte = 13
  DL_PrixUnitaire = 22 500
  DL_MontantHT = 292 500
  DL_CodeTaxe1 = 'C18'
  DL_Taxe1 = 18
  DL_MontantTTC = 345 150
  PF_Num = ''
  CO_No = NULL
  AR_Ref = '' (ligne historique sans article)

Sage F_DOCENTETE :
  DO_TotalHT = 292 500
  DO_Taxe1 = 52 650
  DO_CodeTaxe1 = 'C18'
  DO_TotalTTC = 347 782,50
  DO_NetAPayer = 347 782,50

Local après recalcul :
  montant_ht = 292 500
  montant_tva = 52 650
  montant_ttc = 347 782,50
  additional_cent_tax = 2 632,50
```

#### E4. Cas `FA000003`

```text
Sage F_DOCLIGNE :
  DL_Ligne = 1000
  DL_Qte = 10
  DL_PrixUnitaire = 5 000
  DL_MontantHT = 50 000
  DL_CodeTaxe1 = 'C18'
  DL_MontantTTC = 59 000

Sage F_DOCENTETE :
  DO_TotalHT = 50 000
  DO_Taxe1 = 9 000
  DO_TotalTTC = 59 450
  DO_NetAPayer = 59 450

Local après recalcul :
  montant_ht = 50 000
  montant_tva = 9 000
  montant_ttc = 59 450
  additional_cent_tax = 450
```

#### E5. Ce que le script ne fait pas

- Il ne remappe pas les lignes historiques sans article : `INV-000004` et `FA000003`
  conservent leur désignation snapshot.
- Il ne change pas `sfec_statut`.
- Il ne rejoue pas automatiquement `sfec_pending_writes`.
- Il ne pousse pas de nouvelle facture.

Après réparation :

- vérifier `F_DOCLIGNE` dans Sage ;
- vérifier `F_DOCENTETE` ;
- vérifier `INV-000004` et `FA000003` dans l'IHM ;
- vérifier que le pull suivant ne modifie plus leurs totaux ;
- vérifier `sfec_pending_writes` et purger l'entrée `0-6-INV-000004` si elle n'a plus d'objet.

---

## 5. Tests à ajouter / adapter

### 5.1 Writer Sage

- `test_writer_inserts_pf_num_empty`
- `test_writer_line_numbers_1000_2000`
- `test_writer_sends_null_co_no_when_empty`
- `test_writer_header_computed_from_lines`
- `test_writer_rolls_back_on_line_error`
- `test_writer_completes_existing_orphan_header`
- `test_writer_does_not_duplicate_existing_lines`
- `test_writer_never_deletes_certified_invoice`

### 5.2 Pull

- `test_pull_does_not_overwrite_web_invoice_totals`
- `test_pull_does_not_overwrite_pos_invoice_totals`
- `test_pull_updates_sage_imported_invoice`
- `test_pull_flags_ttc_mismatch_to_monitor`
- `test_pull_ignores_local_invoice_without_lines`

### 5.3 Articles

- `test_fetch_articles_tva_from_f_artcompta`
- `test_fetch_articles_tva_rate_from_f_taxe`
- `test_fetch_articles_unknown_tva_marks_incomplete`
- `test_fetch_articles_unit_from_p_unite`
- `test_fetch_articles_price_ttc_normalized_to_ht`
- `test_article_sync_stores_article_type`
- `test_article_sync_does_not_default_to_18`
- `test_article_sync_periodic`
- `test_products_api_returns_only_sage_articles`
- `test_seed_products_disabled_without_env`

### 5.4 Facture / POS

- `test_invoice_rejects_missing_product`
- `test_invoice_rejects_unsynced_product`
- `test_invoice_rejects_inactive_product`
- `test_invoice_overrides_client_designation`
- `test_invoice_overrides_client_price_and_tva`
- `test_invoice_stores_snapshot_fields`
- `test_invoice_legacy_line_blocks_certification`
- `test_pos_rejects_manual_line_without_product`
- `test_pos_resolves_product_from_sage_catalog`
- `test_billing_form_has_no_free_designation_input` (smoke HTML)

### 5.5 Adaptation des tests existants

De nombreux tests créent des lignes libres, par exemple :

- `tests/test_billing_security.py:9-10` ;
- `tests/test_directory_security.py:7-8` ;
- `tests/test_avoirs.py:60-62` ;
- `tests/test_dashboard_security.py:74-75` ;
- `tests/test_sfec_centimes.py` ;
- `tests/test_pos_security.py:43-44`.

Ils devront utiliser une factory de test qui crée un article `synced_sage=1` avec la
désignation, le prix et la TVA attendus, puis construire la ligne avec `product_id`.
Il ne faut **pas** ajouter un bypass de production pour ces tests.

---

## 6. Critères d'acceptation

### 6.1 Écriture Sage

- Une facture locale à 2 lignes produit :
  - 1 en-tête `F_DOCENTETE` ;
  - 2 lignes `F_DOCLIGNE` ;
  - `PF_Num=''` ;
  - `DL_Ligne=1000` puis `2000` ;
  - `CO_No` NULL ou entier, jamais `''` ;
  - `DO_TotalHT` = somme des lignes ;
  - `DO_Taxe1` = somme des TVA ;
  - `DO_TotalTTC` = TTC lignes + centimes + timbre.
- Une erreur sur la deuxième ligne ne laisse **aucun** en-tête orphelin.
- Un en-tête existant sans ligne est complété, sans doublon.
- Une pièce certifiée n'est jamais supprimée ni recréée.

### 6.2 Pull

- Après pull, `INV-000004` et `FA000003` conservent leurs totaux locaux recalculés.
- Un écart Sage/local supérieur à `1,0 FCFA` produit une anomalie `A_SURVEILLER`.
- Les factures `source='sage'` continuent d'être mises à jour.

### 6.3 Articles et désignations

- Le formulaire de facture ne permet plus de saisir une désignation libre.
- Le POS ne permet plus de créer une ligne libre.
- Toute ligne `web`/`pos` possède un `product_id` Sage actif.
- Une requête forgée avec une désignation, un prix ou une TVA différents est corrigée par
  le serveur (snapshot Sage), pas acceptée.
- La TVA affichée correspond à `F_ARTCOMPTA` + `F_FAMCOMPTA` + `F_TAXE`, pas à un fallback 18 %.
- Les 60 articles de démonstration ne sont plus sélectionnables.
- Les articles inactifs ou non synchronisés ne sont plus sélectionnables.
- Les factures déjà émises ne sont pas réécrites par une modification du catalogue.

### 6.4 Données de démonstration

- `INV-000004` : 1 ligne dans `F_DOCLIGNE`, en-tête HT 292 500, TTC 347 782,50.
- `FA000003` : 1 ligne dans `F_DOCLIGNE`, en-tête HT 50 000, TTC 59 450.
- Totaux locaux recalculés depuis les lignes.
- `sfec_pending_writes` examiné et purgé si l'entrée n'a plus d'objet.
- Aucune nouvelle facture poussée avant validation du correctif.

### 6.5 Qualité

- Suite ciblée actuelle (191 tests) toujours verte.
- Nouveaux tests writer/pull/articles/lignes verts.
- Aucun `data.zip`, `data/` ou `perf-results.jsonl` ajouté.
- Un commit par proposition, staging explicite uniquement.

---

## 7. Séquence de commits proposée

| Ordre | Commit | Contenu |
|---|---|---|
| 1 | `fix(sage): transaction et lignes F_DOCLIGNE` | `PF_Num`, `DL_Ligne`, `CO_No`, transaction, en-tête calculé, idempotence |
| 2 | `fix(sync): ne pas écraser les factures web/pos au pull` | garde `source`, comparaison des totaux, anomalie |
| 3 | `feat(articles): source de vérité Sage` | migration `products`, `fetch_articles`, `_upsert_products`, TVA/unité/type |
| 4 | `feat(invoices): lignes obligatoirement issues de Sage` | résolveur serveur, validation, snapshots |
| 5 | `feat(ui): sélection d'article Sage facture et POS` | formulaire, suppression ligne libre, API filtrée |
| 6 | `chore(seed): désactiver le catalogue de démonstration` | `main.py`, `POST /api/products`, purge seeds |
| 7 | `test: régression writer/pull/articles` | nouveaux tests + adaptation des fixtures |
| 8 | `tools: réparation des pièces Sage orphelines` | script dry-run/`--apply` (hors exécution) |

Le script de réparation ne doit être exécuté qu'après accord explicite et après validation
des commits 1 et 2.

---

## 8. Risques et points à trancher

1. **TVA 20 % dans `BIJOU` vs 18/5/0 SFEC.**
   `F_ARTCOMPTA` + `F_FAMCOMPTA` renvoient `C20` pour 68 articles. Le connecteur SFEC
   n'accepte aujourd'hui que `0/5/18`. Il ne faut pas mapper silencieusement `20` vers `18`.
   Décision requise :
   - `BIJOU` est une base de test non SFEC : les articles à 20 % sont marqués non certifiables ;
   - ou le connecteur doit gérer une base hors Congo avec une configuration de taux ;
   - ou les articles doivent être corrigés dans Sage avant import.

2. **Sémantique de `F_ARTCOMPTA`.**
   L'hypothèse `ACP_Type=0` = vente et `ACP_Champ=1` = tarif par défaut est cohérente avec
   les données observées, mais doit être confirmée avec la documentation Sage / le partenaire.

3. **Mapping `AR_Type` / `AR_Nature` vers `product` / `service`.**
   Le code actuel teste `nature == 1`, or `BIJOU` n'a aucun `AR_Nature=1`.
   Une table de correspondance explicite est nécessaire. Sans validation, le type SFEC peut
   être faux.

4. **Statut actif.**
   `AR_Inactif` n'existe pas dans `BIJOU`. `AR_Sommeil` est le candidat le plus probable,
   mais `AR_Fictif` et `AR_Exclure` ne doivent pas être utilisés sans confirmation.

5. **Lignes historiques sans article.**
   `INV-000004`, `FA000003`, `FA000002`, `FA000001` ont des lignes sans `product_id`.
   Les deux premières sont déjà écrites/certifiées : on ne les remappe pas.
   Les deux dernières devront être remappées ou rester bloquées avant certification.

6. **Réparation `BIJOU`.**
   Toute écriture dans `BIJOU` nécessite un accord explicite. Le script doit être livré en
   `dry-run` et son rapport validé avant `--apply`.

7. **Performance de la recherche d'articles.**
   Le catalogue de production peut contenir des dizaines de milliers d'articles.
   Le formulaire ne doit pas précharger 200 ou 47 000 lignes : recherche serveur avec limite
   et index sur `ref`, `designation`, `barcode`.

8. **Immutabilité.**
   Le snapshot de ligne protège les factures émises, mais il faut conserver la cohérence
   entre `invoice_lines.designation/prix/tva` et les valeurs Sage au moment de l'émission.
   Un contrôle d'audit pourra comparer les snapshots aux articles courants sans les réécrire.

---

## 9. Fichiers impactés

```text
app/integration/sage/writer.py
app/integration/sage/database.py
app/sync/bidirectional.py
app/sync/article_sync.py
app/storage/db.py
app/domain/invoices.py
app/domain/pos.py
app/web/routes/billing.py
app/web/routes/pos.py
app/web/templates/billing/form.html
app/web/templates/pos/index.html
main.py
scripts/repair_sage_orphan_invoices.py      (nouveau)
scripts/purge_seed_products.py              (réutilisation)
tests/test_article_sync.py
tests/test_billing_security.py
tests/test_pos_security.py
tests/test_mirror_totals.py
tests/test_sfec_centimes.py
tests/test_sage_odbc_409.py
tests/...                                   (fixtures communes)
```

---

## 10. Décision demandée

Avant implémentation, valider :

- [ ] la règle « une ligne de facture = un article Sage obligatoire, désignation/prix/TVA
      verrouillés, seule la quantité et la remise sont éditables » ;
- [ ] le traitement des articles à 20 % de `BIJOU` (non certifiables ou configuration
      spécifique) ;
- [ ] le mapping `AR_Type` / `AR_Nature` vers `product` / `service` ;
- [ ] l'autorisation de préparer le script de réparation de `INV-000004` / `FA000003`
      (dry-run uniquement dans un premier temps) ;
- [ ] la priorité : corriger d'abord le writer/pull, puis le référentiel articles, puis l'IHM.
