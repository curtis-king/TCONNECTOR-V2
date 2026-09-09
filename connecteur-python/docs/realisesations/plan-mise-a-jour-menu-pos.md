# Plan de mise à jour — Menu POS (`_sage_hub_sidebar`)

**Objectif :** corriger le désordre de la page `/pos/caisse` **uniquement** via la barre POS `_sage_hub_sidebar()` et son CSS `.sage-hub-*`.

---

## A. Activer l'item courant (le plus impactant)

**Fichier :** `dashboard.py` — `_sage_hub_sidebar()` (l.848-888)

Passer `cur_section` à chaque entrée pour que l'item correspondant à la page affichée soit surligné :

| Ligne menu | Section à passer |
|---|---|
| Vente POS (`/pos/caisse`) | `"caisse"` |
| Devis | `"devis"` |
| Commandes | `"commande"` |
| Livraisons | `"livraison"` |
| Factures | `"vente"` |
| Avoirs | `"avoir"` |

Résultat sur `/pos/caisse` : `<a href="/pos/caisse" class="active">` avec l'accent bleu existant (`.sage-hub-nav a.active`, l.576).

**Vérif :** `Gestion Commerciale` actif + `class="active"` présent dans `.sage-hub-nav` du rendu.

## B. Responsive : finir le « dump » de la sidebar avant le contenu

**Fichier :** `dashboard.py` — CSS `.sage-hub-*` (l.568-583) et media (l.583)

Au lieu de `position:static;max-height:none` en pleine largeur à <1180 px, garder un sous-menu compact :
- `.sage-hub-side-wrap{min-width:0}` (corrige aussi l'absence de règle).
- <1180 px : `.sage-hub-side{max-height:calc(100vh - 40px)}` + `.sage-hub-nav` passe en **barre horizontale à onglets** (grid auto-flow column, scroll-x) pour rester une rangée propre au-dessus de la caisse.

## C. Alignement `hub-head` ↔ `topbar`

**Fichier :** `dashboard.py` — `.sage-hub-side .hub-head` (l.570)
- Uniformiser hauteur (~44 px) et padding avec `.topbar` (l.413) pour éviter le double bandeau décousu.
- Conserver `white-space:nowrap;overflow:hidden` + ellipsis sur le titre long.

## D. Livrable de contrôle visuel
- Régénérer `rendered_caisse.html` (avant/après).
- Générer `rendered_caisse_apres.html` après modification, pour comparaison côte à côte dans le navigateur.

---

## Fichiers touchés
| Fichier | Modification |
|---|---|
| `connecteur-python/dashboard.py` | `_sage_hub_sidebar()` (cur_section) + CSS `.sage-hub-*` (l.568-583) |
| `rendered_caisse.html` / `rendered_caisse_apres.html` | snapshots de validation (artefacts) |

**Hors périmètre :** `.sc-head`/`.sc-kpis`, sidebar bleue `.sidebar`, comportement du ticket.

## Vérifications
1. `python -m py_compile dashboard.py`
2. Rendu `GET /pos/caisse` → HTTP 200 + `class="active"` sur « Vente POS ».
3. Rendu à largeur <1180 px → sous-menu compact, plus de liste pleine largeur.
4. Comparaison visuelle via les 2 snapshots.

---

*✅ Exécuté le 09/09/2026 — validé par le client.*

**Résultat des vérifications :**
- `python -m py_compile dashboard.py` : OK.
- L'item courant s'active sur les 6 pages testées : `/pos/caisse` (Vente POS), `/pos/devis`, `/pos/commandes`, `/pos/livraisons`, `/pos/factures`, `/pos/avoirs` → HTTP 200 + `class="active"` dans `.sage-hub-nav`.
- ⚠️ Sections réelles des routes au **pluriel** (`commandes`, `livraisons`, `ventes`) confirmées et appliquées.
- CSS : `.sage-hub-side-wrap{min-width:0}`, `hub-head` 44 px, media <1180 px → menu POS en rangée d'onglets horizontale à scroll (groupes et icônes masqués).
- Snapshots : `rendered_caisse.html` + `rendered_caisse_apres.html` (mèmes, code final).