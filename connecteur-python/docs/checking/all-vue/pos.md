# Limites & suspicions — Vues Point de vente (`/pos`)

> **Périmètre audité** : `/pos`, `/pos/ticket/<id>/print` + APIs `/api/products*`, `/api/pos/*` et PDF ticket.
> **Méthode** : lecture statique du code réel. Aucun code modifié.
> **Date** : 2026-10-20
> **Fichiers de référence** : `app/web/routes/pos.py`, `app/web/templates/pos/index.html`, `app/web/templates/pos/ticket_print.html`, `app/domain/pos.py`, `app/domain/pdf.py` (`generate_ticket_pdf`), `app/web/routes/sync_api.py`, `app/storage/db.py` (`recalc_ticket_totals`, `calc_line_ticket_totals`), `app/web/auth/security.py`.

## Légende

🔴 bloquant/faux · 🟠 risque réel · 🟡 limite ergo/perf · 🔵 suspicion à vérifier · ⚪ dette/cosmétique

## Synthèse

| Écran | Constats | 🔴 | 🟠 | 🟡/🔵/⚪ |
|---|---|---|---|---|
| Page `/pos` | 15 | 0 | 3 | 12 |
| Ticket en cours / panier | 13 | 1 | 3 | 9 |
| Identité client & facture | 5 | 2 | 1 | 2 |
| Moteur `create_ticket` | 16 | 2 | 6 | 8 |
| Impression / PDF | 10 | 0 | 4 | 6 |
| APIs POS | 7 | 0 | 2 | 5 |

---

## 1. Page `/pos`

Fichiers : `app/web/routes/pos.py:48-134`, `app/web/templates/pos/index.html:1-187`.

### PO-01 🟠 Les statistiques « CA » ignorent toute annulation/remboursement
`count_tickets()` somme les tickets `statut='valide'` (`pos.py:413-424`), or `create_ticket` ne fixe jamais `statut` et aucune route d'annulation/remboursement n'existe. Tous les tickets créés restent donc « valides » : le CA affiché ne peut jamais diminuer après une erreur de caisse.

### PO-02 🟡 « CA du jour » et « Tickets aujourd'hui » utilisent la journée UTC
`date(date_ticket) = date('now')` (`pos.py:418-423`) compare des dates stockées en UTC (`datetime.utcnow()`, `pos.py:34`). Une vente faite après 23 h locale (UTC+1) est comptée sur le lendemain : clôture de caisse fausse.

### PO-03 🟡 « Derniers tickets » limité à 8, sans historique ni filtre
`list_tickets(limit=8)` (`pos.py:50`), rendu sans lien vers un historique, sans filtre vendeur/date et sans numéro cliquable (`pos.py:88-99`). Impossible de retrouver un ticket ancien depuis la caisse.

### PO-04 🟡 Découpage de date fragile dans l'historique
`t.get("date_ticket", "")[11:19]` (`pos.py:93`) suppose un format `YYYY-MM-DD HH:MM:SS`. Un ticket importé avec une date NULL ou un format différent provoque `TypeError`/`None` et casse le rendu de la page.

### PO-05 🟡 « Meilleures ventes » = cumul historique, pas les ventes du jour
`top_selling_products()` (`pos.py:455-489`) joint toutes les lignes `pos_ticket_lines` sans filtrer `statut` ni la date, et ignore les lignes manuelles (`product_id` NULL). Les tuiles affichées ne reflètent donc ni la journée ni les ventes réelles.

### PO-06 🟡 Catalogue figé à 10 tuiles, sans famille ni pagination
`top_selling_products(limit=10)` (`pos.py:51`) puis rendu statique. Aucun filtre par famille, aucune pagination ; au-delà de 10 articles, la recherche/scan est obligatoire.

### PO-07 🟡 Recherche limitée à 20 résultats, sans « voir plus »
`/api/pos/products/search` appelle `search_products(query, limit=20)` (`pos.py:294-296`), sans offset ni indication de troncature. Sur un catalogue Sage volumineux, des articles existants sont invisibles.

### PO-08 🟡 Stock affiché arrondi, incohérent avec le contrôle serveur
`stock_val = int(round(p.get("stock_reel",0) or 0))` (`pos.py:57`) et `makeTileEl` (`index.html:236-239`) arrondissent. Un stock de 0,4 est affiché « 0 / rupture » alors que `check_stock_available` autorise la vente (stock flottant).

### PO-09 🟠 Libellé vendeur non échappé → XSS stockée
`'<option value="{}"{}>{}</option>'.format(v["id"], sel, label)` (`pos.py:104`) insère `prenom`/`nom` bruts. Un vendeur nommé `<img src=x onerror=...>` exécute du script pour tous les caissiers.

### PO-10 🟠 JSON clients injecté brut dans `<script>` → XSS stockée
`pos_clients_js = json.dumps([...])` (`pos.py:109-114`) est rendu via `{{ pos_clients_js | safe }}` (`index.html:180`). Un nom/code client contenant `</script>` casse le script et permet une exécution arbitraire. Même classe que BL-33.

### PO-11 🟠 Fuite de données commerciales + liste clients non bornée
La page expose **tous** les clients avec `niu`, `rccm`, `is_taxable` (`pos.py:106-114`) à tout utilisateur `pos.vente`, alors que le JS n'utilise que `code`/`nom`/`id`. Aucune limite : la page grossit avec l'annuaire et divulgue des identifiants fiscaux.

### PO-12 🟡 Sélection client via `<select>` natif et séparateur `|`
`pos_clients_html` construit `value="|CODE|NOM"` (`pos.py:117-119`) et `getPosClientData` découpe sur `|` (`index.html:189-195`). Un code/nom contenant `|` change le client réel ; l'absence de recherche rend la sélection impraticable avec des centaines de clients.

### PO-13 🟡 « Sync articles » sans retour fiable ni verrou
`syncPosArticles()` fait `alert()` puis `location.reload()` après 2,5 s fixes (`index.html:342`). Le backend lance un thread et répond immédiatement (`sync_api.py:45-56`) : le rechargement peut précéder la fin de sync, et des clics multiples lancent des syncs parallèles.

### PO-14 🟡 Tout caissier peut modifier le contrôle de stock global
`toggleStock()` appelle `/api/pos/config` (`index.html:352`), protégé par `pos.vente` (`security.py:127`) et qui écrit dans la configuration globale (`pos.py:237-252`). Un caissier peut désactiver le contrôle de stock pour tous les postes, sans confirmation.

### PO-15 ⚪ Libellés/ergonomie
`window.NATIVE_ALERTS = true` (`index.html:181`) force les alertes natives ; accents non systématiques (« Qte », « Recu », « Especes ») et `N` au lieu de `N°` dans le ticket.

---

## 2. Ticket en cours / panier

Fichiers : `app/web/templates/pos/index.html:188-374`.

### PO-16 🔴 Quantité négative acceptée → stock gonflé et ticket négatif
`updatePosQte` fait `parseFloat(val) || 1` sans borner (`index.html:292`) ; le `min="0.5"` HTML n'empêche pas la saisie de `-5`. Le serveur ne valide pas non plus : `_decrement_stock` exécute `MAX(stock_reel - (-5), 0)` (`pos.py:512-522`), donc **augmente le stock**, et la ligne produit un total négatif.

### PO-17 🟠 Prix unitaire nul/négatif accepté, aucune validation serveur
`addPosLineManual` fait `parseFloat(prompt(...)) || 0` (`index.html:211-216`) et `create_ticket` ne contrôle ni `quantite` ni `prix_unitaire` (`pos.py:32-90`). Un ticket gratuit ou à montant négatif peut être validé, puis certifié SFEC.

### PO-18 🟡 Ligne manuelle figée à 18 % de TVA
`addPosLineManual` force `taux_tva:18` (`index.html:214`) : pas de choix 0/5/18, pas de remise, pas de code article/compte. Les articles exonérés ne peuvent pas être saisis manuellement.

### PO-19 🟡 Saisie manuelle par `prompt()` bloquant et non validée
Deux `prompt()` successifs (`index.html:212-213`) bloquent l'UI, n'affichent pas le récapitulatif et acceptent texte vide/prix aberrant. Sur mobile, l'expérience est dégradée.

### PO-20 🟡 Fusion du panier uniquement par référence
`addToCart` fusionne si `p.ref` identique (`index.html:198-200`). Les lignes manuelles (`ref:""`) ne fusionnent jamais et un même article sans référence peut apparaître plusieurs fois.

### PO-21 🟡 Quantité 0 transformée en 1 et pas de décimales à l'ajout
`parseInt(...) || 1` (`index.html:196`) puis `if(q<1)q=1` : impossible de saisir 0,5 à l'ajout, et `parseInt` tronque les décimales. Le champ panier accepte pourtant `step="0.5"` (`index.html:251`), incohérence.

### PO-22 🟡 « Reçu / Monnaie » affichés même hors espèces
Le champ `pos-recu` reste actif pour carte/virement (`index.html:169`) et `calcMonnaie` affiche « Total » puis le toast final « Monnaie : 0 FCFA » (`index.html:297-312,337`). Pour un paiement non espèces, cela suggère à tort un rendu de monnaie.

### PO-23 🟡 Mode « Mixte » proposé mais non implémenté
`POS_PAY_METHODS` inclut `mixte` (`index.html:354-361`) avec un seul champ montant : aucune répartition espèces/carte. Côté SFEC, `mixte` retombe sur `bank_transfer` (voir PO-47).

### PO-24 🟠 Contrôle de paiement espèces uniquement côté client
`calcMonnaie` désactive le bouton si `recu < total` (`index.html:303-306`), mais `create_ticket` accepte `montant_recu=0` (`pos.py:32-77`). Un appel direct à `/api/pos/ticket` valide un ticket espèces impayé.

### PO-25 🟡 Aucune confirmation ni garde anti-double validation
`validerTicket` désactive le bouton puis redirige après 1,4 s (`index.html:315-339`), mais l'API n'a pas de clé d'idempotence. Un retry après timeout réseau crée un second ticket + facture + certification.

### PO-26 🟡 Fonctions de caisse absentes
Pas de mise en attente de ticket, pas de tickets multiples, pas de remise globale, pas de recherche client, pas de clôture de caisse. Le POS est limité à une vente séquentielle.

### PO-27 🔵 Arrondis client/serveur potentiellement différents
Le panier arrondit chaque ligne avec `round2` (`index.html:243`) puis additionne ; `recalc_ticket_totals` peut arrondir autrement. Un écart de quelques centimes est possible entre le total affiché et le total stocké/imprimé.

### PO-28 ⚪ `clearCart` ne réinitialise pas tout
`clearCart()` vide le panier (`index.html:293`) mais ne remet à zéro ni `pos-recu`, ni le vendeur, ni le mode de paiement ; seul le succès de validation le fait partiellement (`index.html:338`).

---

## 3. Identité client & facture

Fichiers : `app/web/templates/pos/index.html:189-195,315-326`, `app/domain/pos.py:92-126,229-240`.

### PO-29 🔴 Le client sélectionné n'est jamais rattaché à la facture
`getPosClientData` renvoie `id:null` pour tous les clients réels (`index.html:193`), donc `contact_id` est toujours `null` (`index.html:323`). La facture POS n'est jamais liée au contact, même quand le caissier a choisi un client.

### PO-30 🔴 NIU/RCCM/type fiscal jamais transmis → professionnel certifié comme particulier
`validerTicket` n'envoie que `tiers_nom` et `tiers_code` (`index.html:321-326]). `_create_invoice_for_ticket` applique `data.get("tiers_type","particulier")` (`pos.py:116,235`) et ne reçoit aucun NIU/RCCM. Une vente B2B est donc certifiée SFEC comme un particulier, avec le NIU par défaut.

### PO-31 🟠 `is_taxable` chargé mais ignoré
`pos_clients_js` expose `is_taxable` (`pos.py:112`) mais ni `getPosClientData` ni la création de facture ne l'utilisent. Aucune exonération client n'est possible au POS.

### PO-32 🟡 Traitement incohérent du « Client comptoir »
Le cas spécial renvoie l'`id` du contact `CLI-CPT` (`index.html:191`), alors que tous les autres clients renvoient `null`. Selon l'existence du contact comptoir, `contact_id` est renseigné ou non.

### PO-33 🟡 Aucune création/édition de client depuis la caisse
Le POS renvoie vers l'annuaire ; en cas de client inconnu, la vente est forcément faite au comptoir. Limite opérationnelle en boutique.

---

## 4. Moteur `create_ticket`

Fichier : `app/domain/pos.py:32-310`.

### PO-34 🔴 Aucune validation métier des lignes côté serveur
`create_ticket` accepte `quantite`, `prix_unitaire`, `taux_tva`, `remise_pct` et `montant_recu` bruts (`pos.py:39-46,535-568`). Quantités/prix négatifs, taux arbitraires et remises > 100 % passent : la base et SFEC peuvent recevoir des montants incohérents.

### PO-35 🔴 Écriture multi-étapes non transactionnelle
Le ticket, ses lignes, les totaux, le stock et la facture sont écrits dans des `get_cursor()` successifs (`pos.py:48-84`), sans transaction englobante. Un échec au milieu laisse un ticket sans lignes, un stock décrémenté sans facture, ou une facture sans ticket.

### PO-36 🟠 Contrôle de stock non atomique → survente concurrente
`check_stock_available` lit le stock ligne par ligne (`pos.py:491-509`) puis `_decrement_stock` décrémente plus tard (`pos.py:512-522`). Deux caisses peuvent valider la même dernière unité (check-then-act race).

### PO-37 🟠 Stock plafonné à 0, le déficit est perdu
`MAX(stock_reel - ?, 0)` (`pos.py:519`) empêche le stock négatif. Si le contrôle est désactivé, une survente ne laisse aucune trace de manque et l'inventaire devient faux.

### PO-38 🟠 Stock décrémenté même si la facture échoue
`_decrement_stock` s'exécute (`pos.py:79`) avant `_create_invoice_for_ticket` (`pos.py:81`). Si la facture échoue, le ticket reste valide et le stock est déjà diminué, sans compensation.

### PO-39 🟠 Échec de création de facture avalé silencieusement
`_create_invoice_for_ticket` attrape toutes les exceptions et renvoie `{invoice_id:None,...}` (`pos.py:296-310`) ; `create_ticket` renvoie quand même `success:True` (`pos.py:85-90`). Le caissier voit un ticket valide alors qu'aucune facture/SFEC n'existe.

### PO-40 🟠 Numéros fournis/générés sans garantie d'unicité
`numero = data.get("numero") or generate_ticket_number()` (`pos.py:33`) accepte un numéro client ; `generate_invoice_number()` est appelé ensuite (`pos.py:95`). Une collision lève `IntegrityError` après l'insertion du ticket et la décrémentation du stock (PO-35), sans reprise.

### PO-41 🟡 Date du ticket en UTC
`datetime.utcnow()` (`pos.py:34,97`) décale l'horodatage par rapport à l'heure locale imprimée et aux clôtures de caisse.

### PO-42 🟡 Conversions `float()` non maîtrisées
`float(data.get(...))` sur `montant_recu`, remises, quantités et prix (`pos.py:39-46`) lève `ValueError` sur texte ; la route renvoie alors 400 avec le message brut. Les valeurs négatives, elles, passent.

### PO-43 🟡 Remise globale présente côté moteur, absente de l'UI
`remise_globale_pct` / `remise_globale_montant` sont lus (`pos.py:43-44`) et utilisés par `recalc_ticket_totals`, mais `validerTicket` ne les envoie jamais (`index.html:320-326`). Fonctionnalité morte côté caisse.

### PO-44 🟠 `additional_cent_tax` codé en dur à 5 %, avec doute explicite
`additional_cent_tax = round(total_tax_t_amount * 0.05, 2)  # a verifier: 5% TVA 18% seulement ?` (`pos.py:194`). Le centime additionnel est calculé sur **toutes** les TVA non 5/0, sans règle métier validée.

### PO-45 🟠 Tout taux autre que 0/5 est agrégé dans la TVA 18 %
`if taux_tva == 5 ... elif taux_tva == 0 ... else total_tax_t_amount += montant_tva` (`pos.py:168-173`). Un taux personnalisé est déclaré à SFEC/Sage comme du 18 %, alors que la ligne garde son taux réel.

### PO-46 🟡 Statut et reste à payer figés
La facture POS est insérée avec `statut='a_comptabiliser'`, `valide=1`, `montant_restant=0` (`pos.py:104-111`) quel que soit le mode de paiement. Pas de vente à crédit, pas de suivi de règlement.

### PO-47 🟡 Mode de paiement inconnu ou « mixte » → virement bancaire
`_map_payment_method` retourne `bank_transfer` par défaut (`pos.py:19-21`). « mixte » n'est pas mappé : la certification SFEC déclare un virement au lieu d'un paiement mixte.

### PO-48 🟡 Devise et notes ignorées
`devise` vaut `data.get("devise","XAF")` (`pos.py:98`) mais l'UI ne l'envoie jamais ; `notes` du ticket (`data.get("notes")`) n'est pas recopié dans la facture. Toute vente POS est en XAF et sans note.

### PO-49 🔵 Échecs de synchronisation seulement journalisés
`log_sync(...)` (`pos.py:83,208`) écrit une trace mais `create_ticket` n'expose pas d'échec de stock/SFEC/Sage autrement que par des champs optionnels. Pas de file de reprise visible.

---

## 5. Impression ticket / PDF

Fichiers : `app/web/routes/pos.py:137-233`, `app/web/templates/pos/ticket_print.html`, `app/domain/pdf.py:392-490`.

### PO-50 🟠 Message de ticket injecté sans échappement → XSS stockée
`message=get_setting("ticket_message", ...)` (`pos.py:230`) puis `{{ message | safe }}` (`ticket_print.html:50`). Le paramètre de configuration peut injecter du HTML/JS dans chaque ticket imprimé.

### PO-51 🟠 QR code injecté sans échappement dans `src`
`qr` est seulement vérifié par préfixe `data:`/`http` et longueur (`pos.py:197`), puis interpolé brut dans `<img src="{}">`. Une réponse SFEC `http://x" onerror="..."` exécute du script sur la page d'impression.

### PO-52 🟠 Logo/base64 injecté brut
`logo_path` et `company["logo_base64"]` sont insérés sans échappement (`pos.py:157-159`). Un chemin contenant `"` casse l'attribut ; une valeur base64 altérée injecte du HTML (configuration compromise).

### PO-53 🟡 Impression automatique imposée après 500 ms
`<body onload="setTimeout(function(){window.print()},500)">` (`ticket_print.html:24`). L'utilisateur ne peut pas consulter le ticket avant impression ; les impressions par lot et certains navigateurs mobiles échouent silencieusement.

### PO-54 🟡 Identifiants séquentiels et lien PDF sans `noopener`
`/pos/ticket/<int:id>/print` expose tout ticket à tout utilisateur authentifié (`pos.py:137-141`), sans contrôle de caisse/jour. Le lien PDF `target="_blank"` (`ticket_print.html:54`) n'a pas `rel="noopener"`.

### PO-55 🟡 Reçu/monnaie imprimés pour tous les modes
Le ticket affiche toujours `recu` et `monnaie` (`ticket_print.html:44-48`), y compris carte/virement, où ces montants n'ont pas de sens.

### PO-56 🟡 Montants arrondis à l'unité dans l'impression
Prix et totaux sont formatés `{:,.0f}` dans le HTML (`pos.py:147-150`) et le PDF (`pdf.py:439-443`). Un prix de 12,5 est imprimé 12/13 selon l'arrondi, écart avec la facture.

### PO-57 🟠 Champs libres injectés dans des `Paragraph` ReportLab
`company.name`, `ticket.numero`, `tiers_nom`, `caissier`, désignations et message sont passés bruts (`pdf.py:416-433,477`). Un `&` ou `<` dans un nom client fait planter la génération PDF (500) ou injecte du balisage.

### PO-58 🟡 PDF A5, libellé FCFA et date UTC codés en dur
`generate_ticket_pdf` utilise `A5` (`pdf.py:398`) alors que le HTML imprime en 80 mm (`ticket_print.html:23`) ; `"FCFA"` est en dur (`pdf.py:456-459`) et la date est celle du ticket UTC. Rendu thermique et devises non couverts.

### PO-59 🔵 Bloc facture/SFEC silencieusement vide ou trompeur
Si `invoice_engine.get_invoice` échoue, `invoice_html` reste vide (`pos.py:185-211`) sans avertissement. Si SFEC est désactivée, le ticket affiche indéfiniment « certification en attente... » (`pos.py:206-208`).

---

## 6. APIs POS

Fichier : `app/web/routes/pos.py:23-360`.

### PO-60 🟠 `limit` non validé : 500 sur texte, pagination contournable sur valeur négative
`min(int(request.args.get("limit",200)),500)` (`pos.py:30,272`) lève `ValueError` sur `limit=abc` (500). Avec `limit=-1`, SQLite interprète `LIMIT -1` comme **aucune limite** : `/api/pos/tickets?limit=-1` ou `/api/products?limit=-1` retourne tout le contenu.

### PO-61 🟠 Les APIs produits exposent le prix d'achat
`list_products`, `search_products`, `find_product_by_barcode` font `SELECT *` (`pos.py:691-712,432-453`) et les routes renvoient la ligne complète (`pos.py:23-34,294-315`). Tout utilisateur `pos.vente` récupère `prix_achat` et le stock dans la réponse réseau, même si l'UI ne les affiche pas.

### PO-62 🟡 Pagination incomplète
`list_tickets` supporte `offset` (`pos.py:353-355`) mais la route ne le transmet pas (`pos.py:266-275`) ; `total` de `/api/products` vaut `len(products)` (taille de page) et non le total correspondant (`pos.py:34`). Impossible de paginer ou de connaître le vrai volume.

### PO-63 🟡 Aucune idempotence sur la création de ticket
`/api/pos/ticket` (`pos.py:254-262`) crée systématiquement ticket + facture. Un retry client après timeout duplique la vente et la certification SFEC.

### PO-64 🟡 Messages d'exception renvoyés bruts au client
Les routes renvoient `str(e)` (`pos.py:42,261,340`), exposant chemins SQLite, noms de tables et détails internes.

### PO-65 🟡 Gestion du contrôle de stock sans permission dédiée
`/api/pos/config` est protégé par `pos.vente` (`security.py:127`) et modifie la configuration globale (`pos.py:237-252`). Aucun rôle « responsable caisse » distinct.

### PO-66 ⚪ Pas de tests ni de journal d'audit caisse
Aucun test dédié au POS ; les actions sensibles (désactivation stock, création produit, ticket) ne sont tracées que par des `logger.info`. Pas de piste d'audit en cas de litige.

---

## 7. Suspicions prioritaires à trancher

1. **PO-30/PO-29** : confirmer avec une vente B2B réelle que le NIU client n'est jamais transmis à SFEC (impact fiscal direct).
2. **PO-16/PO-17/PO-34** : vérifier si des tickets à quantité ou prix négatifs existent déjà en base (`SELECT * FROM pos_tickets WHERE montant_ttc < 0`).
3. **PO-35/PO-38/PO-39** : rechercher des tickets sans `invoice_id` ou des factures POS orphelines ; mesurer l'ampleur des écritures non transactionnelles.
4. **PO-44/PO-45** : valider la règle du centime additionnel et le traitement des taux ≠ 0/5/18 avec la DGI/SFEC.
5. **PO-60** : tester `?limit=abc` et `?limit=-1` sur les endpoints publics authentifiés.
6. **PO-50/PO-51/PO-52/PO-10/PO-09** : confirmer la présence de caractères dangereux dans les noms clients/vendeurs et les réponses SFEC.
7. **PO-05/PO-01** : décider si le POS doit gérer annulation/remboursement et clôture de caisse, ou si ces fonctions restent dans Sage.
