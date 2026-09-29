# Deep research — Pourquoi une requête peut prendre 2 minutes (et comment l'éliminer)

> Audit 2026-09-24, 2 agents lecture seule (cycle de vie requêtes + runtime/infra).
> Référence : `C:\Users\tp_m2\Documents\projects\TCONNECTOR-V2\connecteur-python`.
> Complète `plan-optimisation-bigO.md` (qui traite le Big-O) : ici, **tout le reste** — verrous, timeouts, polling, serveur.

---

## 1. Réponse courte : les 2 minutes sont un empilement, pas un bug unique

```
1 requête lente  =  attente verrou (sync en cours)      ~30-60s
                  + N+1 Sage sérialisé sous lock global ~100s / 500 factures
                  + 1 timeout SFEC (30s) + polling 12×2s ~54s
                  + probe connectivité 17s
                  + 8 threads waitress saturés → file d'attente
                  ≈ 2 minutes. Normal, pas anormal — avec l'archi actuelle.
```

---

## 2. Les 8 causes, rangées par impact

### C1. Certif SFEC : 54 s par facture, en boucle (`endpoints.py:433-434,602-603`)
`POST timeout=30s` + si pas de numéro : `12 × (sleep(2s) + GET)` = jusqu'à **414 s théoriques, ~54 s typiques** par facture.
`certify_pending_pos_invoices` et `auto_certify` multiplient par N (pool 3-5, `.result()` attend le plus lent).
**Fix** : `/api/sfec/certify` + POS → `202` + worker de fond ; interactif réduit à `timeout=10s`, `range(5)`, `sleep(1)`.

### C2. Pagination SFEC sans borne (`engine.py:250-260,332-345`, `client.py:131-145`)
`while True, page_size=500`, pas de `max_pages` → 10 000 factures = **20 GET séquentiels** (10 s si rapide, 600 s si timeouts),
exécutés **2-3× par cycle** + re-scan complet à chaque 409.
**Fix** : lookup partagé + cache négatif ; filtre serveur `?invoice_number=` au lieu du scan client ; budget max pages.

### C3. Sage N+1 sous **double verrou** (`database.py:677,960` + `_db_lock:268` + `engine._lock:194`)
`sync_all` tient le lock moteur pendant **tous** les fetchs ; chaque facture = 1 ODBC lignes. 500 factures × 200 ms = **100 s**
pendant lesquelles tous les `get_cache()` des pages web font la queue.
**Fix** : batch `DOCLIGNE IN (...)` (cf. plan Big-O O1) + ne plus tenir `engine._lock` sur les I/O réseau + `arraysize=1000` (jamais défini aujourd'hui).

### C4. Réseau synchrone dans les pages (`dashboard.py:31,99,145`, `sync_api.py:104`)
`GET /` fait `ping SFEC + métriques Sage + probe connectivité` en série ; `/pending` fait `list_invoices(500)` **dans la requête**
(~47 s pire cas) ; `POST /api/sync/bi` lance `full_sync()` **dans le worker HTTP**.
**Fix** : pages sur cache uniquement (`get_cache()`), SFEC en fond, `/api/sync/bi` → `Thread + 202` (comme `/api/sync:38-42`).

### C5. Probe connectivité à chaque appel SFEC (`connectivity.py:25-49`, TTL 10 s)
DNS 3×2 s + HTTP 3+3+5 s = **jusqu'à 17 s** avant même l'appel, repayés à chaque page de pagination et chaque POST.
Pas de `requests.Session`, pas de circuit breaker.
**Fix** : TTL plus long + skip si frais, `Session` + retry adapter.

### C6. Pool ODBC inexistant : 1 connexion + 1 lock pour tout (`database.py:22-24,229-282`)
`pool_min:2,pool_max:10` de la config **jamais lus**. Web + polling + bi-sync + articles se partagent 1 handle.
Les 8 threads waitress ne servent à rien en dessous.
**Fix** : vrai pool (`queue.Queue`, 4-8 connexions), lock seulement autour du checkout, timeouts curseur = `request_timeout_ms`.

### C7. SQLite : 1 connexion partagée, `synchronous=FULL`, `ensure_schema()` par requête
`db.py:18-48` : une connexion pour tous les threads (`timeout=10`, `busy_timeout=5s`) → `database is locked` sous charge
sync+web ; `user_auth` rejoue le DDL/migrations **à chaque login et chaque audit** (`user_auth.py:214-455`) ;
chaque requête écrit (audit, `log_sync`, `recalc`, numérotation).
**Fix** : connexions par thread ou mini-pool, `synchronous=NORMAL, busy_timeout=30000, cache_size=-64000`,
DDL une fois au boot, bufferiser l'audit.

### C8. Logs INFO par ligne + polling qui se marchent dessus
`RotatingFileHandler` + INFO par table/ligne (`database.py:409-452`, `endpoints.py:413-420`) ; 3 boucles
(polling 30 s + bi-sync 15-60 s + prewarm au boot) frappent Sage+SFEC+SQLite en même temps ;
`GET /api/metrics` refait une requête Sage live à chaque hit frontend.
**Fix** : prod en `warning`, per-row en `debug`, métriques sur cache, floor bi-sync 60 s.

---

## 3. Ce que ça donne pour une "grande boîte" (×10 volumes)

| Volume | Aujourd'hui (pire cas) | Après C1→C8 + plan Big-O |
|---|---|---|
| Sync 5 000 factures Sage | ~17 min (N+1 × 200 ms) | ~1 min (batch + pool) |
| Certif 50 factures lentes | ~45 min (50 × 54 s) | file async, UI libre en 200 ms |
| Liste SFEC 10 k | 10-600 s, 3×/cycle | 1 lookup partagé + filtre serveur |
| Page `/billing` chargée | 15 scans + verrous | 1 agrégation, < 500 ms |
| 8 requêtes concurrentes | file derrière les locks | pool 8 + pas de réseau en page |

## 4. Ordre d'exécution recommandé (au-delà du Big-O)

1. **Sortir le réseau des requêtes** (C4) + **certif async** (C1) — effet démo immédiat, 0 risque données.
2. **Vrai pool ODBC + lock resserré** (C6) + **batch lignes** (plan O1).
3. **Bornes SFEC** (C2 : max pages, cache, filtre serveur) + **connectivité** (C5).
4. **SQLite prod** (C7 : pool/thread-local, `synchronous=NORMAL`, DDL au boot) + **logs warning** (C8).
5. Puis plan Big-O O2→O4 (déjà chiffré).

**Gate "grandes boîtes"** : sync 5k < 2 min, page < 500 ms sous 8 users concurrents, 0 `database is locked`, 0 timeout HTTP visible.
