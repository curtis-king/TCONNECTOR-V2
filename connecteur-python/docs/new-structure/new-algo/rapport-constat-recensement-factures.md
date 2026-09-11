# Rapport de constat — Système de recensement des factures (SFEC / Sage)

| | |
|---|---|
| **Date** | 09/09/2026 |
| **Périmètre** | `connecteur-python` (branche `refactor/restructure-connecteur-python`) |
| **Objet** | Audit du système qui recense / rapproche les factures entre Sage 100 et la plateforme SFEC |
| **Type** | Constat uniquement (aucune modification de code) |

---

## 1. Objet et méthode

Ce rapport documente l'existant du « recensement des factures » : l'ensemble des parcours qui
énumèrent les factures certifiées (ou à certifier) côté SFEC et côté Sage, puis les
rapprochent dans les deux sens.

Méthode : lecture statique du code (sans exécution), identification de chaque point de parcours
de liste, calcul de complexité et chiffrage des appels réseau en conditions nominales.

Fonctions impliquées :

| Fonction | Fichier | Rôle |
|---|---|---|
| `sync_sfec_invoices()` | `app/sync/engine.py:238` | Recense toutes les factures certifiées SFEC (pagination complète) |
| `_build_sfec_lookup()` | `app/sync/engine.py:321` | Re-parcourt la liste SFEC pour construire `numero → invoice` (TTL 300 s) |
| `reconcile_sfec_status()` | `app/sync/engine.py:400` | Rapprochement Sage → SFEC (tous les 8 cycles de polling) |
| `auto_certify_invoices()` | `app/sync/engine.py:509` | Détermine les factures restant à certifier (via le lookup) |
| `sync_incremental()` | `app/sync/engine.py:272` | Met à jour le cache local des factures Sage |
| `verify_by_invoice_number()` | `app/integration/sfec/client.py:129` | Recherche « un numéro » en scannant toute la liste SFEC |
| `pull_from_sage()` | `app/sync/bidirectional.py:85` | Rapprochement SQLite ↔ Sage (requête N+1) |
| `fetch_certified_invoices()` | `app/integration/sage/database.py:1454` | Liste les certifiées côté Sage (1 requête SQL) |
| Page « En attente » | `app/web/dashboard.py:860` | Recensement partiel (page 1 / 500) |

---

## 2. Vue d'ensemble du système

```
                       ┌─────────────────── SFEC (API serveur) ───────────────────┐
                       │  GET /api/v1/invoices?page=N&pageSize=500                  │
                       └──────────────────────────────┬────────────────────────────┘
                                                      │
        ┌────────────────────────────┬────────────────┴─────────────┬───────────────────────┐
        │                            │                              │                       │
   sync_sfec_invoices          _build_sfec_lookup           verify_by_invoice_number      pending page
   (crawl complet)             (re-crawl complet)           (scan linéaire)                (page 1/500)
        │                            │                              │                       │
        └──────────── cache          │                              └── scan complet ────────┘
                       │              │
                       ▼              ▼
            _cache["sfec_invoices"]   lookup dict numero→invoice
                       │
        ┌──────────────┴───────────────┬───────────────────────────────┐
        │                              │                               │
   auto_certify_invoices        reconcile_sfec_status            _process_retry_queue
   (à certifier)                (Sage → SFEC)                    (retour / relance)
        │                              │
        ▼                              ▼
   Sage 100 (SQL Server) ────────── fetch_certified_invoices / read_invoices_from_sage
```

Le **constat central** : le même jeu de données (la liste des factures certifiées sur SFEC)
est **re-téléchargé intégralement, à plusieurs endroits, dans les deux directions de
rapprochement, sans jamais utiliser la pagination incrémentale déjà supportée par l'API**.
Chaque source est « lue du début à la fin » de façon répétée : c'est une traversée
bidirectionnelle *complète* (Sage → SFEC **et** SFEC → Sage, à chaque cycle).

---

## 3. Constats détaillés

### Constat 3.1 — Recensement SFEC par pagination intégrale (`sync_sfec_invoices`)

`app/sync/engine.py:238` parcourt **toute** la liste certifiée, page par page (500/page),
du début à la fin, à partir de `page=1` :

```python
page = 1
while True:
    result = client.list_invoices(page=page, page_size=500)
    batch = result.get("invoices", [])
    if not batch:
        break
    all_invoices.extend(batch)
    total = result.get("total", result.get("total_count", 0))
    if page * 500 >= total:
        break
    page += 1
```

Déclencheurs : préchauffage au démarrage (`app/sync/scheduler.py:54`), chaque `sync_all()`,
et le bouton manuel `/api/sfec/sync` (`app/web/dashboard.py:1657`).

**Conséquence** : un recensement complet coûte `⌈S/500⌉` requêtes HTTP **séquentielles**
(chaque GET attend le précédent). Pour `S = 5 000` factures certifiées → **10 requêtes par
recensement**, soit environ `timeout(30 s) × 10` dans le pire cas, à chaque recensement.

### Constat 3.2 — Re-crawl complet pour le lookup `numero → invoice` (`_build_sfec_lookup`)

`app/sync/engine.py:321` refait **strictement la même pagination** (depuis `page=1`) pour
construire un dictionnaire `numero → invoice`, avec un TTL de 300 s (`engine.py:45-46`).

Appelé depuis 4 endroits distincts, chacun goûtant le TTL :

| Appelant | Fréquence |
|---|---|
| `_process_retry_queue()` (`engine.py:106`) | à chaque cycle de polling si retry en file |
| `auto_certify_invoices()` (`engine.py:534`) | à **chaque** cycle de polling (défaut 30 s) |
| `reconcile_sfec_status()` (`engine.py:418`) | tous les 8 cycles (≈ 4 min) |
| `certify_single()` sur 409 (`engine.py:706`) | à la demande |

**Conséquence** : en polling 30 s, le lookup est « rafraîchi » par un **re-crawl complet**
toutes les 5 minutes → ~12 re-crawls/heure, **en plus** du crawl de `sync_sfec_invoices`.
Deux traversées complètes et autonomes du même endpoint, avec des buts quasi identiques
(le lookup construit les mêmes données que le crawl, sans en profiter).

**Chiffrage** (pour `S = 5 000`, `pageSize = 500`, soit 10 pages/crawl) :

| Activité | Requêtes/h | Requêtes/jour |
|---|---|---|
| Lookup complet (TTL 5 min) | 12 × 10 = 120 | ≈ 2 880 |
| Crawl `sync_sfec_invoices` (démarrage + sync_all) | 1 à 2 | ≈ 10-20 |

Or `config.example.json:62` définit `rate_limit.daily_max = 2500` et `per_minute_max = 100`,
**mais cette limite n'est appliquée nulle part dans le code**. À 2 880 requêtes/jour, le
plafond quotidien configuré est dépassé : risque réel de blocage de la clé API SFEC.

### Constat 3.3 — Recherche unitaire par numéro = scan linéaire (`verify_by_invoice_number`)

`app/integration/sfec/client.py:129` recherche **un** numéro de facture en parcourant la
liste SFEC **page par page depuis la première**, jusqu'à trouver la correspondance :

```python
page = 1
while True:
    result = self.list_invoices(page=page, page_size=500)
    invoices = result.get("invoices", [])
    if not invoices:
        break
    for inv in invoices:
        if seller_num == invoice_number or inv_num == invoice_number:
            return inv
    if page * 500 >= total:
        break
    page += 1
```

Complexité en **O(⌈S/500⌉)** requêtes HTTP **dans le pire cas** (numéro absent = parcours
complet). Appelé pour chaque facture en erreur 409 (`app/integration/sfec/endpoints.py:490`)
et pour chaque élément de la file de retry (`engine.py:365`).

**Conséquence** : en cas de rejet massif (ex. 200 factures en 409), c'est jusqu'à
`200 × S/500` requêtes — soit 2 000 GET pour `S = 5 000` et 200 factures — alors qu'une
recherche dans un index en mémoire serait O(1).

### Constat 3.4 — Rapprochement bidirectionnel `reconcile_sfec_status`

`app/sync/engine.py:400` :
- direction **Sage → SFEC** : lit toutes les certifiées Sage (`fetch_certified_invoices`, 1 requête SQL), et pour chacune fait un lookup dans le dict SFEC (O(1), bien) ;
- mais pour obtenir ce dict, il **re-va chercher toute la liste SFEC** (constat 3.2).

Le direction inverse (**SFEC → Sage**) est assurée par `sync_sfec_invoices` (constat 3.1),
qui remplit un cache `sfec_invoices` **que rien d'autre ne consomme réellement**. Le
recensement est donc effectivement **bidirectionnel et dupliqué**.

### Constat 3.5 — Cache local Sage : inserts en tête + scans linéaires

`sync_incremental()` (`engine.py:272`) :
- réinsère chaque nouvelle facture en tête de liste → `insert(0, inv)` = **O(N)** par insert (`engine.py:289`, `298`) ;
- reconstruit l'index `{id: i}` à chaque cycle alors qu'il n'est utilisé que dans la boucle (`engine.py:283`, `292`).

Des **parcours linéaires** O(N) du cache sont utilisés pour retrouver une facture par id :
- `_process_retry_queue()` (`engine.py:126-129`), appelé une fois par élément traité ;
- `certify_single()` (`engine.py:657-661`), à chaque certification manuelle.

### Constat 3.6 — Rapprochement SQLite ↔ Sage en N+1 (`pull_from_sage`)

`app/sync/bidirectional.py:85` lit **toutes** les factures Sage
(`read_invoices_from_sage`, `app/integration/sage/writer.py:98` — scan complet de
`F_DOCENTETE` à chaque cycle de 60 s), puis pour **chacune** exécute une requête individuelle :

```python
cur.execute("SELECT id FROM invoices WHERE numero = ?", (numero,))
```

Soit **T requêtes SQL** par cycle (T = nombre total de factures Sage), au lieu d'une seule
requête groupée. Idem dans `certify_pending_pos_invoices` et `push_to_sage`.

### Constat 3.7 — Page « En attente » : recensement tronqué

`app/web/dashboard.py:860` fait `list_invoices(page=1, page_size=500)` pour marquer les
certifiées, mais **ignorers factures au-delà de la première page** :
- les certifiées > 500 apparaissent comme « en attente » → l'utilisateur peut re-certifier → cycle 409 ; 
- et c'est une (autre) traversée de la liste SFEC à chaque affichage de la page.

`/api/sfec/certified-from-api` (`dashboard.py:1667`) lit `page_size=100`, incohérent avec le
`500` employé partout ailleurs.

---

## 4. Analyse de complexité

Notations : `S` = certifiées SFEC, `C` = certifiées Sage, `T` = factures ventes Sage,
`N` = taille du cache `sales_invoices`, `K` = nouveaux incréments.

| Opération (actuelle) | Coût réseau | Coût CPU/mémoire |
|---|---|---|
| Recensement SFEC (`sync_sfec_invoices`) | O(⌈S/500⌉) GET séquentiels × répété au démarrage/sync_all | O(S) mémoire (liste entière) |
| Lookup SFEC (`_build_sfec_lookup`) | O(⌈S/500⌉) GET × ~12/h | O(S) mémoire (dict) |
| `verify_by_invoice_number` | O(⌈S/500⌉) GET par appel (pire cas) | O(1) |
| `reconcile_sfec_status` | O(⌈S/500⌉) GET (via lookup) + 1 SQL | O(S + C) |
| `sync_incremental` | 0 | O(N) × K (inserts en tête) + O(K) index |
| Recherche facture par id dans le cache | 0 | O(N) par recherche |
| `pull_from_sage` | 0 | O(T) requêtes SQL + O(T log N) |

**Élément dominant** : le coût réseau O(⌈S/500⌉) répété ~12×/h **sans jamais utiliser**
`date_start` / `date_end` / `page_size` alors que ces paramètres sont **déjà implémentés et
simplement jamais appelés** (`app/integration/sfec/client.py:94-105`).

---

## 5. Risques

1. **Débit API SFEC** : re-crawl complet toutes les 5 min ⇒ risque de dépassement du quota
   (`rate_limit.daily_max=2500` défini mais jamais appliqué).
2. **Latence** : recensements UTC séquentiels (~S/500 × 30 s max par GET) ralentissent le
   polling (un cycle de recensement peut bloquer `auto_certify`).
3. **Boucles 409** : recensement tronqué de la page « En attente » + scan linéaire de
   `verify_by_invoice_number` sur des factures déjà certifiées.
4. **Cohérence** : deux cache distincts (`sfec_invoices` et le lookup dict) remplis par deux
   crawls indépendants ; risque de divergence et d'inutilité d'un des deux.
5. **Pas d'incrémental malgré une API aujourd'hui/plage de dates** : tout est re-téléchargé
   alors que seule la « bosse » depuis une date/watermark aurait besoin d'être rattrapée.

---

## 6. Pistes d'optimisation algorithmique

### 6.1 — Watermark / curseur de dates (backfill une fois, puis Δ)

L'API accepte `date_start` et `date_end` (déjà dans `client.py:94`, jamais utilisés).
**Algorithme** : un backfill complet initial (taille `S_0`), puis un curseur
`cursor = max(certification_date, id)` persisté (config ou base SQLite), et chaque crawl ne
ramène que `date_start >= cursor`. Coût : O(Δ/500) GET par cycle au lieu de O(S/500), avec
`Δ ≪ S` en régime établi. C'est l'évolution la plus importante.

### 6.2 — Un seul index en mémoire maintenu en continu (hash join)

Remplacer `_build_sfec_lookup` + `sync_sfec_invoices` par **une unique structure**
`revue: {list: [...], by_numero: {num: inv}, by_id: {sfec_id: inv}, watermark}` :
- maintenue lors de chaque certification locale (append) et de chaque crawl incrémental (constat 6.1) ;
- jamais reconstruite « depuis zéro » ;
- lecture O(1) partout (au lieu des scans O(N) et des re-crawls O(S/500)).

Supprime aussi les scans linéaires du cache Sage : un `sales_by_id` dict maintenu à
jour dans `sync_incremental` (insert en tête O(N) → append + mise à jour index O(1)).

### 6.3 — Méthode des deux pointeurs pour la réconciliation

`reconcile_sfec_status` : trier (une seule fois, par `numero`) Sage certifiés et SFEC
list, puis fusionner à deux pointeurs :
```
i, j = 0, 0
while i < len(A) and j < len(B):
    compare A[i].numero et B[j].numero
    # égal → OK ; A < B → certifiée Sage absente SFEC (écart) ; B < A → certifiée SFEC absente Sage
```
Complexité de détection **O(C + S)** sans requêtes réseau additionnelles après 6.2.

### 6.4 — Requêtes groupées (batch) à la place du N+1

`pull_from_sage` : remplacer les T requêtes `WHERE numero=?` par une seule
`SELECT id, numero FROM invoices WHERE numero IN (?,?,...)` (batch 500) ou par une lecture
des numéros Sage en un seul set, puis différence d'ensembles en mémoire (set subtraction :
`nouveaux = sage_num ≠ sqlite_num`). O(T) requêtes → O(T/500) requêtes.

### 6.5 — Vérification en mémoire avant tout appel réseau

Avant un `certify` / `verify_by_invoice_number`, vérifier la présence dans l'index
`by_numero` (O(1)). Éviter 200 GET dans le scénario de rejet 409 décrit au constat 3.3.
Utiliser aussi un **set d'ids certifiées en vol** pour éviter les doubles soumissions.

### 6.6 — Filtre de Bloom optionnel

Pour les très gros volumes (`S > 100 000`), un filtre de Bloom en mémoire
(faux positifs acceptables, puis confirmation par GET ciblé `get_invoice(id)` de O(1))
permet de qualifier « vue / jamais vue » sans chargement exhaustif.

### 6.7 — Page de garde débit (token bucket)

Implémenter le `rate_limit` déjà documenté dans `config.example.json:62` : burst autorisé
`per_minute_max=100`, quota quotidien `daily_max=2500`. Complète 6.1 (le régime incrémental
fait tomber la consommation sous le quota).

---

## 7. Recommandation (ordre de priorité)

1. **Watermark incrémental (6.1)** — le gain dominant : O(Δ/500) au lieu de O(S/500) par cycle.
2. **Un seul index SFEC maintenu en continu (6.2)** — supprime les doublons de crawls et les scans O(N).
3. **Réconciliation à deux pointeurs (6.3)** — O(C+S) sans réseau supplémentaire.
4. **Batch SQL dans `pull_from_sage` (6.4)** — élimine le N+1.
5. **Vérif mémoire avant API (6.5)** — tue les boucles 409 et les `verify_by_invoice_number` coûteux.
6. **Garde-fou `rate_limit` (6.7)** — protection compte API.
7. **Bloom filter (6.6)** — uniquement si montée en charge future.

---

## 8. Conclusion

Le recensement actuel repose sur des **traversées bidirectionnelles intégrales** : la liste
SFEC est re-téléchargée en pagination complète par deux mécanismes autonomes (`sync_sfec_invoices`
et `_build_sfec_lookup`), la liste Sage est entièrement relue chaque cycle, et le rapprochement
se fait par scans linéaires ou requêtes en N+1 — alors que l'API SFEC offre déjà les
paramètres de plage (`date_start`/`date_end`) et que les techniques classiques (watermark,
hash/index, merge à deux pointeurs, batch, filtre de Bloom, token bucket) s'appliquent
directement à ce schéma. Aucune modification de code n'a été apportée : ceci est un constat
préalable à l'implémentation d'une optimisation.