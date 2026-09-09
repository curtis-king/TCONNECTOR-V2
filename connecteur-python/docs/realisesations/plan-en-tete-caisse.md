# Plan de travail — Refonte de l'en-tête de la caisse `/pos/caisse`

**Fichier cible :** `connecteur-python/dashboard.py` — fonction `pos_page()` (ligne ~3153)
**Zone concernée :** bloc `sc-toolbar` + bloc `sc-toolbar-stats` (le « graphique » tickets / CA)

---

## 1. Constat / problèmes actuels

L'en-tête actuel mélange tout sur une seule rangée flex :

```
[Nouveau] [+ Ligne manuelle] | [Sync articles] [Stock: --] ......... [3 stats]
```

1. **Désordre** : boutons et 3 statistiques se bousculent dans la même ligne (`margin-left:auto` sur les stats) ; dès que la ligne déborde, `flex-wrap` envoie les statistiques n'importe où.
2. **Stats sans hiérarchie** : trois `<span>` identiques, textes en dur avec couleur inline `style="color:#0f172a"`, pas d'icône, pas de libellé structuré.
3. **Nombre de tickets non formaté** : `{tj}` sort en clair (`1250` au lieu de `1 250`), alors que les montants FCFA sont formatés.
4. **Verticalité inégale** des 4 boutons et du séparateur `sc-sep` ; le bouton « Stock: -- » porte des classes `btn-success/btn-warning` qui n'existent pas dans le CSS de la caisse.
5. **Aucune gestion responsive propre** de cette zone.

---

## 2. Structure cible (2 rangées propres)

### Rangée A — Barre de titre + actions
```
┌──────────────────────────────────────────────────────────────────────┐
│  [icon] SAISIE DE CAISSE      [Nouveau] [+ Ligne] [Sync] [Stock: ON] │
│         24/06/2026 · Depot 001                                        │
└──────────────────────────────────────────────────────────────────────┘
```
- Gauche : titre de page + sous-titre contextuel.
- Droite : groupe d'actions aligné (base commune, mêmes hauteurs/rayons).

### Rangée B — 3 tiroirs statistiques (KPI) alignés, formats identiques
```
┌────────────┬─────────────┬─────────────┐
│ Tickets    │ CA du jour  │ CA total    │
│ du jour    │             │             │
│  1 250     │ 250 000 FCFA│ 45 500 000  │
│  (icone)   │  (icone)    │  FCFA       │
└────────────┴─────────────┴─────────────┘
```

---

## 3. Modifications prévues

### 3.1 Python — `stats_html` (dans `pos_page`)
- Remplacer les 3 `<span>` inline par la nouvelle structure `sc-kpis` :
  - `<div class="sc-kpi sc-kpi-blue">` → Tickets du jour
  - `<div class="sc-kpi sc-kpi-green">` → CA du jour
  - `<div class="sc-kpi sc-kpi-navy">` → CA total
- **Formatage des nombres** avec séparateur d'espace fine (même style que `_fmt_money`) :
  - tickets : `1 250` (et non `1250`)
  - montants : `250 000` puis « FCFA ».
- Suppression de toute couleur inline (passage en classes CSS).
- Ajout de petites icônes (blocs `ico`) par carte : ticket, billets, total.

### 3.2 Python — bloc HTML `sc-toolbar` (dans `pos_css` / body)
- Nouvelle structure :
  ```
  <div class="sc-head">
    <div class="sc-head-title">
      <div class="sc-head-ico">...</div>
      <div><div class="sc-head-name">Saisie de caisse</div>
            <div class="sc-head-sub">date du jour · depot</div></div>
    </div>
    <div class="sc-actions"> (4 boutons conservés, mêmes `onclick`/`id`) </div>
  </div>
  <div class="sc-kpis">@@STATS@@</div>
  ```
- **Aucun changement des identifiants JS** : `clearCart()`, `addPosLineManual()`, `syncPosArticles()`, `btn-stock`/`toggleStock()`, `refreshStockBtn()` restent identiques.
- Le séparateur `sc-sep` est supprimé (remplacé par un `gap` propre).

### 3.3 CSS (`pos_css`)
| Classe | Rôle |
|---|---|
| `.sc-head` | Rangée titre : `flex`, `align-items:center`, `justify-content:space-between`, fond blanc cassé + bordure fine. |
| `.sc-head-ico` | Petite puce bleu dégradé (30 px) avec « € »/icône. |
| `.sc-head-name` | Titre 15 px gras. |
| `.sc-head-sub` | Sous-titre 11 px gris (date, dépôt). |
| `.sc-actions` | Groupe : `gap:8px`, `flex-wrap:wrap`, boutons à hauteur fixe (34 px), `white-space:nowrap`. |
| `.sc-kpis` | Barre : `display:grid; grid-template-columns:repeat(3,1fr); gap:10px`, avec en-tête cohérent. |
| `.sc-kpi` | Carte unitaire : fond blanc, bordure, rayon 6 px, `display:flex` icône + libellé + valeur. |
| `.sc-kpi-ico` | Cercle coloré (40 px) aligné verticalement. |
| `.sc-kpi-val` | Valeur 18 px gras, couleur de la variante. |
| `.sc-btn` | Uniformisation : `min-height:34px`, `gap`, `font-size:12px`. |
| `.sc-btn.on/.sc-btn.off` ou `btn-success/btn-warning` | Style ON/OFF du stock propre (vert/ambre) au lieu de classes inexistantes. |

### 3.4 Responsive
- `@media(max-width:1180px)` : KPI → 3 colonnes resserrées ; actions passent sous le titre (`justify-content:flex-start`).
- `@media(max-width:760px)` : KPI → 1 colonne empilée ; le groupe de boutons passe en 2 colonnes.

---

## 4. Vérifications / tests

1. `python -m py_compile dashboard.py` → OK.
2. Rendu via client Flask `GET /pos/caisse` → HTTP 200.
3. Vérifier la présence de `.sc-head`, `.sc-kpis`, 3 `.sc-kpi`, valeurs formatées (`1 250`),
   absence de couleur inline.
4. Vérifier que tous les hooks JS restent intacts (recherche des `id` dans la page rendue).
5. **Livrable de contrôle visuel** : générer un aperçu statique
   `rendered_caisse.html` (boutons + KPI) pour validation rapide dans le navigateur.

---

## 5. Fichiers touchés

| Fichier | Modification |
|---|---|
| `connecteur-python/dashboard.py` | `pos_page()` : `stats_html`, bloc `sc-toolbar`, `pos_css` |
| `rendered_caisse.html` (artefact de contrôle) | généré, supprimé ensuite |

**Hors périmètre** (inchangés) : le panier, le catalogue, la validation de ticket, les API `/api/pos/*`, les autres pages.

---

*✅ Exécuté le 09/09/2026 — validé par le client.*

**Résultat des vérifications :**
- `python -m py_compile dashboard.py` : OK.
- Rendu `GET /pos/caisse` : HTTP 200.
- KPI affichés et formatés (`0`, `0 FCFA`, `622 148 FCFA`), date du jour injectée (`09/09/2026 · Depot 001`).
- Anciennes classes (`sc-toolbar-stats`, `sc-sep`, couleurs inline) supprimées ; hooks JS (`btn-stock`, `toggleStock`, `refreshStockBtn`, `clearCart`, etc.) intacts.
- Aperçu statique : `connecteur-python/rendered_caisse.html` (à supprimer après contrôle).