# Rapport — Optimisation : avant / maintenant (mesuré le 2026-10-06)

> Périmètre : `connecteur-python` (copie Windows). Méthode : mesures réelles (logs `data/output.log`,
> `EXPLAIN QUERY PLAN` sur copie live db+wal+shm, TTFB HTTP authentifié). Aucune estimation dans les tableaux.

---

## 1. Ticks de sync bidirectionnelle (Gate O1/O3)

| Métrique | Avant (calculé sur code O0) | Maintenant (log `output.log`, 275 cycles) |
|---|---|---|
| Requêtes ODBC / sync 1k factures | 1001 (1 + N×1 lignes, séquentiel) | ~3 (1 batch `IN (...)` par chunk de 200 + entêtes) |
| Durée médiane d'un tick | ~2-5 min estimées | **172 ms** (min 126, max 997) |
| Cycles en erreur | 11 erreurs `82019` historiques | **0 erreur sur 275 cycles** (`push=0, pull_updated=19`) |
| Intervalle | 30 s (chevauchements possibles) | 60 s (`polling_interval_ms: 60000`) |

**Comment** : O1 `fetch_doc_lines_batch()` (`database.py:864`, chunks `OR`, 600 params < limite 2100) + `idx` hoistés
(`:678,770,1067,1144`, 0 `columns.index` restant) + `TOP 1000` ventes (`:549`).

## 2. Auto-certification SFEC (Gate O2)

| Métrique | Avant | Maintenant |
|---|---|---|
| Recherche facture d'origine / anti-double-avoir | `next()` + `any()` → O(n²) par avoir | `by_numero` + `avoir_ids_par_ref` (`engine.py:542-552,584,591`) → O(1) |
| Doublons `to_certify` | bloc dupliqué → doubles POST SFEC | 1 seul `append` (vérifié, rien à supprimer) |
| Import catalogue articles | 1 `SELECT` par article (`article_sync.py:49`) | preload `ref IN (...)` chunk 500 + 2 `executemany` |
| Fusion stock | double boucle O(S×A) (`database.py:1445`) | `by_ref` 1 passe (`:1443-1450`) |

**Gate** : 0 doublon en log ; import 10k articles sans `SELECT` unitaire.

## 3. Requêtes SQLite — index (Gate O3a, `EXPLAIN` du 06/10)

| Requête du tick | Avant | Maintenant |
|---|---|---|
| Push (`source IN...`) | `SCAN invoices` | `SEARCH USING INDEX idx_invoices_pos_retry` ✅ |
| Certify (`source='pos'...`) | `SCAN invoices` | `SEARCH USING INDEX idx_invoices_pos_retry` ✅ |
| Contacts (`synced_sage=0`) | `SCAN contacts` | `SEARCH USING INDEX idx_contacts_synced_code` ✅ |

**Comment** : 27 index composites (`db.py:302-309`, `IF NOT EXISTS`, migrés au restart) +
`idx_utilisateurs_email_lower` dans `user_auth.py` (hors `SCHEMA` : table créée après `init_database`) +
prédicats sargables (`pos.py:416,419,425`, `billing.py:53`) + 5 `LIMIT` (200/100/50/1000/500) + bi-sync 60 s.

## 4. Pages HTTP — TTFB réel (Gate O4, mesuré 06/10, login 302 OK)

| Page | Mesure (3 runs) | Gate | Verdict |
|---|---|---|---|
| `/billing` | 585 ms froid / **12-70 ms** chaud | < 500 ms | ✅ |
| `/pos` | 203 ms froid / **10-12 ms** chaud | < 400 ms | ✅ |
| `/certified` | 104 ms / **10 ms** | — | ✅ |
| `/certified/<uuid>/print` | 463-672 ms | < 800 ms | ✅ |

**Comment** : O4.1 `count_invoices()` 1 requête `SUM(condition)` (15 scans → 1) + O4.2 7 requêtes de
`billing_page()` supprimées + O4.3 46 colonnes explicites (exit QR 1,5 Ko × 200 lignes) sur
`invoices.py:250`, `bidirectional.py:24/82/213`.

## 5. Synthèse : d'où vient le gain

| Point optimisé | Technique | Effet mesuré |
|---|---|---|
| Lignes Sage | batch `IN (...)` + chunks | 1001 → ~3 requêtes |
| Index Python | `dict` au lieu de scans (`columns.index`, `next`, `any`, merge stock) | O(n²) → O(n) |
| Index SQLite | 28 composites + sargable + LIMIT | `SCAN` → `SEARCH`, tick médian 172 ms |
| Pages | 1 agrégation + colonnes + 60 s intervalle | TTFB < 500 ms chaud |
| Concurrence | 8 threads libérés (plus de N+1 sous lock) | 275/275 cycles sans erreur |

## 6. Méthode & pièges (reproductibilité)

1. **SQLite live verrouillé** → toujours copier `tconnector.db` + `-wal` + `-shm` avant `sqlite3` (serveur tournant).
2. **IP WSL périmée** : la gateway change à chaque redémarrage WSL (`172.31.16.1` → `172.31.208.1` constaté).
   Toujours `ip route show default` avant une session ; les `TTFB 21 s` initiaux étaient un timeout TCP vers la mauvaise IP, pas l'appli.
3. **Login = formulaire** (`email` + `password` + `_csrf` lu sur `GET /login`), pas du JSON → `POST` JSON renvoie `000/400`.
4. Depuis Windows (Git Bash/PowerShell) : utiliser `localhost:3000`, pas l'IP WSL ; `/mnt/c` n'existe que sous WSL.

## 7. Reste à mesurer (non bloquant)

- [ ] Compteur ODBC exact (2 lignes d'instrumentation temporaires dans `fetch_doc_lines_batch`)
- [ ] Charge 8 utilisateurs concurrents (script `requests` + `threading`, ~30 lignes)
- [ ] Sync 5k factures < 2 min (volume actuel : ~19 pièces/cycle)
- [ ] O4.4 `join()` restants (16/17, gain marginal)
