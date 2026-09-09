# Rapport d'audit — Page `/pos/caisse` (sidebar POS / `_sage_hub_sidebar`)

**Date :** 09/09/2026
**Contexte :** le désordre remonte à la barre latérale POS (« Gestion Commerciale »), générée par `_sage_hub_sidebar()` — pas à l'en-tête de ticket (`.sc-head`/`.sc-kpis` déjà validé) ni au sidebar bleu global.

---

## 1. Piège confirmé : les noms sont trompeurs

Deux barres latérales coexistent et leurs noms de classes se ressemblent :

| Élément | Rôle | Code |
|---|---|---|
| `.sidebar` (bleu global) + `.sidebar-nav` | Navigation principale de l'app (Dashboard, Factures…) | `dashboard.py:374-393` |
| `.sage-hub-side` + `.sage-hub-nav` | **Menu POS** « Gestion Commerciale » des pages `/pos/*` | `dashboard.py:568-583` |

**Erreur précédente :** mes corrections de débordement d'icônes ont ciblé `.sidebar-nav .lbl/.ico/.cnt` (le sidebar bleu `dashboard.py:488-502`), pas `.sage-hub-*` (568-583). Le vrai coupable du désordre est le menu POS.

---

## 2. Structure réelle de la page (basée sur `rendered_caisse.html`)

```
<body>
  <div class="sidebar">            <- bleu global repliable (76-250 px)
  <div class="main-content">
    <div class="topbar">
      <div class="topbar-title">Vente POS</div>   <- titre global
      (… boutons Sync / user / logout)
    </div>
    <div class="sage-hub-layout">                  <- grille : 230px 1fr
      <div class="sage-hub-side-wrap"></div>      <- AUCUNE règle CSS
        <div class="sage-hub-side">
          <div class="hub-head">Gestion Commerciale</div>
          <ul class="sage-hub-nav"> (groupes + 15 entrées) </ul>
        </div>
      <div class="sage-hub-main">                 <- la caisse (sc-head, sc-kpis…)
    </div>
```

---

## 3. Problèmes constatés (avec preuves)

### 3.1 — Aucun item du menu POS n'est jamais « actif »
Dans `_sage_hub_sidebar()` (`dashboard.py:848-888`), `_li()` compare `cur_section == section`, mais **aucun appel ne passe `cur_section`** :
```python
nav.append(_li("/pos/caisse", "caisse", "Vente POS"))   # cur_section absent
```
`_li` a `cur_section=None` → `None == "caisse"` → jamais actif.
**Preuve rendu :** sur `/pos/caisse`, le seul `class="active"` du document est l'item `/pos` de la sidebar bleue globale ; il n'y a **aucun** `active` dans `.sage-hub-nav`.

### 3.2 — À <1180 px, la sidebar POS « envahit » le haut de page
`dashboard.py:583` :
```css
@media(max-width:1180px){.sage-hub-layout{grid-template-columns:1fr}
                         .sage-hub-side{position:static;max-height:none}}
```
→ la liste arborescente complète (8 groupes, ~15 entrées) se déroule en pleine largeur **avant** la caisse = le « bordel » signalé en haut de page.

### 3.3 — `.sage-hub-side-wrap` sans CSS
Aucune règle pour ce conteneur (`grep` : 0 résultat) : ni `min-width:0`, ni largeur, ni marges — il dépend uniquement de la grille `230px 1fr`.

### 3.4 — Double bandeau de titre
`hub-head` « Gestion Commerciale » + `topbar-title` « Vente POS » : deux en-têtes empilés sans cohérence de hauteur/alignement.

---

## 4. Éléments NON concernés (à ne pas toucher)
- `.sc-head` / `.sc-kpis` (en-tête de caisse refait, validé).
- La sidebar bleue globale `.sidebar` / `.sidebar-nav`.
- Le panier, catalogue, validation ticket, API `/api/pos/*`.

## 5. Référence d'inspection
- `connecteur-python/rendered_caisse.html` — snapshot complet (sidebar POS + header), re-généré depuis le code actuel le 09/09/2026.