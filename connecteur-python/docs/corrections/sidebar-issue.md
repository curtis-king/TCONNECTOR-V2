# Correction : Sidebar qui ne se replie pas au clic

## Symptôme

Cliquer sur le bouton toggle de la sidebar ne replie pas visuellement la sidebar :
rien ne bouge tant que la souris reste sur la sidebar, et le repli n'apparaît
qu'après avoir quitté la zone.

## Cause racine

La règle `.sidebar.collapsed:hover` dans `app/web/static/css/app.css` (ligne 23)
ré-étend la sidebar à 250px tant que le curseur est sur elle, et elle a priorité
sur `.sidebar.collapsed{width:76px}` (ligne 4) car elle est déclarée plus tard et
à spécificité égale. Comme le bouton toggle se trouve dans la sidebar, au moment
du clic la souris est dessus → la classe `collapsed` est bien ajoutée mais la
largeur reste à 250px. En plus, une fois repliée, le bouton toggle est masqué
(`.sidebar.collapsed .sidebar-toggle{display:none}`, ligne 20), donc impossible
de la rouvrir autrement que par le survol.

Le JS `toggleSidebar()` (app.js:95) est correct ; le souci est purement CSS.

## Solution

Modifier `app/web/static/css/app.css` :

1. Supprimer la ligne 20 : `.sidebar.collapsed .sidebar-toggle{display:none}`
   → le bouton reste visible une fois repliée pour permettre de la rouvrir.
2. Supprimer le bloc `.sidebar.collapsed:hover` et ses surcharges enfants
   (lignes 23-31) → plus d'expansion au survol, repli permanent et immédiat.
3. Remplacer la ligne 19
   `.sidebar.collapsed .sidebar-top{justify-content:center;padding:18px 0 14px}`
   par `.sidebar.collapsed .sidebar-top{justify-content:space-between;padding:18px 16px 14px}`
   → le logo et le toggle restent correctement alignés.

Ce qui reste inchangé :
- `.sidebar.collapsed{width:76px}` (ligne 4)
- Masquage des labels : `.sidebar.collapsed .sidebar-logo span`,
  `.sidebar.collapsed .sidebar-nav .nav-label`,
  `.sidebar.collapsed .sidebar-nav li a .label`,
  `.sidebar.collapsed .sidebar-foot .net-indicator span:not(.net-dot)`
  (lignes 13-16) et centrage des icônes (lignes 17-18)
- Décalage du contenu `.sidebar.collapsed + .main-content{margin-left:76px}` (ligne 21)
- JS `toggleSidebar()` (app.js:95) et `common.py`

## Résultat attendu

Au clic sur le toggle, la sidebar se replie immédiatement à 76px et y reste
(plus de ré-expansion au survol). Le bouton reste visible pour la rouvrir.
L'état est mémorisé dans `localStorage` via `tconn_sidebar`.