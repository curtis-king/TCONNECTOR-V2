# Rapport des réalisations

**Module :** Connecteur - `connecteur-python`
**Fichier principal modifié :** `dashboard.py`
**Période :** Septembre 2026
**Objectif global :** Stabiliser et refondre l'interface de caisse et la gestion des factures certifiées SFEC (Système de Facturation Électronique Certifié - Congo-Brazzaville).

---

## 1. Correction du bug : articles introuvables dans la caisse

**Problème** : Sur la page `/pos/caisse`, les articles (BIJOU) ne s'affichaient plus / le chargement échouait.

**Actions :**
- Analyse comparative entre le modèle SQL local et la configuration de la base (`config.json`, SQLite `data/tconnector.db`).
- Désambiguïsation du séparateur décimal : la base SQLite utilise la virgule (`1,5`) en configuration francophone, ce qui cassait les conversions numériques.
- Normalisation du calcul/affichage des montants pour que les articles BIJOU remontent correctement depuis le stock.

---

## 2. Refonte de la page caisse `/pos/caisse`

**Objectif** : Reproduire le fonctionnement de la saisie de caisse décentralisée de Sage 100C.

**Réalisé :**
- Nouveau layout en grille façon Sage 100C :
  - Zone catalogue/stock (produits BIJOU).
  - Panier de vente avec lignes dynamiques.
  - Bloc total / rendu monnaie.
  - Touches rapides (F1, F2...).
- Conservation des identifiants et fonctions JavaScript attendus pour ne pas casser le flux existant (`fn_monnaie`, etc.).
- **Test effectué** : rendu via client Flask (HTTP 200, 10 tuiles produits présentes).

---

## 3. Correction des débordements - sidebar « Gestion Commerciale »

**Problème** : les icônes de la barre latérale dépassaient du cadre (icônes trop hautes, libellés mal coupés, badges entiers débordant).

**Actions (CSS dans `dashboard.py`) :**
- `max-height: calc(100vh - 40px)` + `overflow-y: auto` sur la nav bleue.
- Icônes `.ico` limitées à 17 px avec `overflow: hidden`.
- Libellés dans `<span class="lbl">` : `white-space: nowrap` + ellipsis.
- Badges `.cnt` : `flex-shrink: 0` pour ne jamais être compressés.
- Fonction de rendu des liens (`_li`) adaptée.

---

## 4. Correction des débordements - haut de page

**Problème** : le haut de page créait du désordre avec les icônes (logo restant visible sur la sidebar repliée, barre supérieure cassée).

**Actions :**
- Sidebar repliée (collapsed) : logo masqué, bouton hamburger centré.
- `.main-content` : marge gauche réduite à **76 px** quand la sidebar est repliée.
- `.topbar` : `flex-wrap: wrap` pour éviter les chevauchements.

---

## 5. Refonte de la page Certifications `/certified`

**Objectif** : page listant les factures certifiées SFEC, plus claire et adaptée aux écrans.

**Réalisé :**
- En-tête de page (`page-header`) : breadcrumb « SFEC / Certifications », bouton de synchronisation SFEC, indicateur de statut.
- **3 cartes de statistiques** :
  - `c-tot` : nombre total de certifications ;
  - `c-aff` : nous avons refusé / en attente... nombre de certifications affichées ;
  - `c-mont` : montant total certifié (calculé dans `renderCert()` en JavaScript).
- Carte « Filtres » en **thème clair** (suppression des styles sombres inline) :
  - Recherche, statut, date de début, date de fin.
- Pagination repassée en thème clair.
- **Tests** : compilation Python + rendu HTTP 200 via le client Flask.

---

## 6. Refonte de la facture SFEC `print_certified`

**Route** : `/certified/<invoice_id>/print`

### 6.1 Helpers de formatage ajoutés (après `_esc`)

| Fonction | Rôle |
|---|---|
| `_to_num(val)` | Conversion robuste en nombre (supprime espaces, NBSP, virgule → point). |
| `_fmt_money(val)` | Format monétaire : séparateurs de milliers (`1 500 550`), 0 décimale, arrondi automatique. |
| `_fmt_qty(val)` | Format quantité : entier sans décimale (50) sinon 2 décimales à virgule française (2,50). |
| `_nw_cent(n)` | Nombre en lettres pour 0–99 (gère soixante-dix, quatre-vingt, etc.). |
| `_nw_mille(n)` | Nombre en lettres pour 0–999 (cent, deux cents, etc.). |
| `_nw_amt(n)` | Nombre en lettres complet jusqu'à 999 999 999 (million(s), mille). |

**Exemples validés :**
- `_fmt_money(1500550)` → `1 500 550`
- `_fmt_money(0)` → `0`
- `_fmt_qty(2.5)` → `2,50`
- `_nw_amt(1500550)` → `un million cinq cents mille cinq cent cinquante`

### 6.2 Corrections de format

- **Bug « format mal les 0 »** : les totaux étaient insérés bruts (`_esc(...)`) sans séparateurs de milliers. Remplacés par `_fmt_money`.
- Quantités des lignes : `_fmt_qty` (plus de `50.0`).
- Ligne de synthèse ajoutée : *« Arrêtée la présente facture à la somme de **Un million cinq cents mille cinq cent cinquante (1 500 550 XAF)**. »*

### 6.3 Nouveau design (A4, prêt à imprimer)

- En-tête dégradé bleu « corporate » : raison sociale, adresse, **NIU**, **RCCM** + bloc FACTURE (numéro).
- Bandeau vert : *« Certifiée par le SFEC - document juridiquement valable (décret n° 2026-101 du 31 mars 2026) »*.
- 4 KPI : Numéro, Date, Devise, Mode de paiement.
- Blocs « Vendu à » (nom, NIU, adresse, téléphone) et « Paiement » (mode, net à payer).
- Tableau des lignes : Désignation, Qté, Prix unitaire HT, TVA %, Montant HT (lignes zébrées).
- Totaux : Total HT, TVA 18 %, TVA 5 %, **TOTAL TTC** (bandeau sombre).
- Bloc **Certification SFEC** : statut, date, signature courte, signature complète.
- Section **QR code** de vérification d'authenticité.
- Doubles cases **Signature & cachet** (Client / Vendeur).
- Pied de page + boutons « Imprimer / PDF » et « Retour à la liste » masqués à l'impression (`@media print`, `@page A4`).

**Tests :** `python -m py_compile dashboard.py` OK ; rendu HTTP 200 via client Flask avec une facture simulée ; vérification du HTML (montants, QR, montant en lettres, absence de placeholders `{}` parasites).

---

## 7. Recherche : format de facture Congo-Brazzaville

Pour caler la facture sur la réglementation locale, une recherche web a confirmé :

- **SFEC** = Système de Facturation Électronique Certifié, plateforme **PGSFEC**.
- Modes d'émission : e-Facture / API / TFC / TCC.
- Cadre réglementaire : **décret n° 2026-101 du 31 mars 2026**.
- **TVA** : taux normal **18 %**, taux réduit **5 %** (ciment, verre, gasoil, gaz butane, sucre, tomate, savon, huile, etc.). Devise : **XAF / FCFA**.
- **Mentions obligatoires** d'une facture :
  1. Numéro chronologique continu ;
  2. Date d'émission ;
  3. Identité du vendeur : raison sociale, adresse, **NIU**, **RCCM** ;
  4. Identité du client : nom, **NIU**, adresse ;
  5. Désignation des articles ;
  6. Quantité ;
  7. Prix unitaire HT ;
  8. Taux de TVA ;
  9. Montant de TVA ;
  10. Total TTC ;
  11. Case signature / cachet ;
  12. « Arrêtée la présente facture à la somme de … » ;
  13. Numéro de certification + signatures courte et complète SFEC ;
  14. QR code de vérification d'authenticité.

Ces mentions sont toutes intégrées dans la nouvelle facture `print_certified`.

---

## 8. Tests et vérifications

| Vérification | Résultat |
|---|---|
| `python -m py_compile dashboard.py` | OK |
| Rendu `/pos/caisse` (client Flask) | HTTP 200, 10 tuiles |
| Rendu `/certified` (client Flask) | HTTP 200 |
| Rendu `/certified/<id>/print` (facture simulée) | HTTP 200, HTML vérifié |
| Helpers de formatage (unitaires) | Conformes (cf. exemples) |

---

## 10. Refonte de l'en-tête de la caisse `/pos/caisse` (09/09/2026)

**Objectif** : aligner et nettoyer le bloc supérieur de la caisse (boutons + statistiques en désordre).

**Réalisé :**
- **Rangée titre** `.sc-head` : icône gratuit + « Saisie de caisse » + sous-titre `09/09/2026 · Depot 001` (date du jour injectée côté Python).
- **Rangée actions** `.sc-actions` : les 4 boutons (Nouveau, + Ligne manuelle, Sync articles, Stock) alignés à droite, hauteur uniforme (32 px), `gap` propre ; suppression du séparateur `.sc-sep`.
- **Barre KPI** `.sc-kpis` : 3 cartes alignées avec icône SVG, libellé et valeur :
  - Tickets du jour (bleu), CA du jour (vert), CA total (bleu marine).
  - Couleurs inline `style="color:#0f172a"` supprimées (classes CSS).
  - Nombres formatés avec séparateur d'espace insécable (`622 148 FCFA`, plus de `1250` brut).
- **Bouton Stock** : styles propres `.sc-btn.btn-success / .btn-warning` (au lieu de classes inexistantes), JS `toggleStock()`/`refreshStockBtn()` inchangés.
- **Responsive** : 1180 px (resserrement), 760 px (KPI 1 colonne, titre et actions empilés).
- **Vérifications** : compile OK, rendu HTTP 200, hooks JS intacts, aperçu `rendered_caisse.html` généré pour contrôle visuel.

**Fichier :** `dashboard.py` — `pos_page()` (ligne ~3153) + `stats_html` + `pos_css`.

---

## 11. Fichiers / artefacts

**Principale modification :**
- `connecteur-python/dashboard.py`

**Artefacts temporaires (nettoyés après usage) :**
- `connecteur-python/data/dump_page.py`, `data/verify_cert.py`, `data/test_nw.py`, `data/test_print.py`
- `connecteur-python/rendered_print.html` : aperçu de la nouvelle facture (à retirer si non souhaité).

**Prochaines étapes suggérées :**
- Redémarrer le serveur (port **3000**) pour appliquer les modifications en production.
- Vérifier l'affichage des lignes de TVA « incluse » selon l'article, pour affiner l'écran de caisse.
- Tests de bout en bout sur les flux e-Facture / API du portail PGSFEC.