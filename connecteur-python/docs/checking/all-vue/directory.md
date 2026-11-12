# Limites & suspicions — Annuaire (`/clients`, `/vendeurs`, `/utilisateurs`)

> **Périmètre audité** : `/clients`, `/vendeurs`, `/utilisateurs` + APIs `/api/contacts*`, `/api/vendeurs*`, `/api/utilisateurs*`, `/api/auth/*`.
> **Méthode** : lecture statique du code réel. Aucun code modifié.
> **Date** : 2026-10-20
> **Fichiers de référence** : `app/web/routes/directory.py`, `app/web/routes/sync_api.py:117-154`, `app/web/templates/directory/{clients,vendeurs,utilisateurs}.html`, `app/domain/pos.py` (`create_contact`, `list_contacts`, `create_vendeur`, `update_vendeur`, `list_vendeurs`, `get_vendeur_stats`), `app/web/auth/user_auth.py`, `app/web/auth/security.py`.

## Légende

🔴 bloquant/faux · 🟠 risque réel · 🟡 limite ergo/perf · 🔵 suspicion à vérifier · ⚪ dette/cosmétique

## Synthèse

| Écran | Constats | 🔴 | 🟠 | 🟡/🔵/⚪ |
|---|---|---|---|---|
| Clients `/clients` | 16 | 2 | 5 | 9 |
| Vendeurs `/vendeurs` | 12 | 1 | 3 | 8 |
| Utilisateurs `/utilisateurs` | 13 | 0 | 3 | 10 |
| APIs & permissions | 8 | 1 | 2 | 5 |

---

## 1. Clients & fournisseurs (`/clients`)

Fichiers : `app/web/routes/directory.py:163-201`, `app/web/templates/directory/clients.html`, `app/domain/pos.py:617-689`.

### DI-01 🔴 Champs contact injectés sans échappement dans le tableau → XSS stockée
`rows` interpole `code`, `typ`, `nom`, `niu`, `email`, `telephone`, `ville` bruts (`directory.py:180-194`), puis `{{ rows | safe }}` (`clients.html:25`). Un contact importé de Sage (ou créé via API avec `type` arbitraire) contenant `<img onerror=...>` exécute du script chez tous les utilisateurs de l'annuaire.

### DI-02 🔴 `contacts_json` injecté brut dans `<script>` → XSS stockée
`contacts_json=json.dumps(contacts)` (`directory.py:197`) rendu par `var contacts = {{ contacts_json | safe }}` (`clients.html:57`). Un nom/NIU contenant `</script>` casse la page et permet une exécution arbitraire, sans interaction (même classe que BL-33 et PO-10).

### DI-03 🟠 Total et compteurs faux au-delà de 500 contacts
`list_contacts(limit=500)` (`directory.py:164`), puis `total=len(contacts)` et `nb_total=len(contacts)` (`directory.py:195`). Le titre « Contacts ({{ total }}) » et les cartes Clients/Fournisseurs n'affichent donc que la page, pas le référentiel réel.

### DI-04 🟡 « Fournisseurs » compte tous les types non `client`
`else: fourni_count += 1` (`directory.py:168-170`). Un type vide, `NULL`, `prospect` ou `both` est compté comme fournisseur, faussant les statistiques.

### DI-05 🟠 Recherche limitée aux 500 contacts déjà chargés
`filterContacts()` masque des lignes DOM (`clients.html:59-66`) ; aucune requête serveur, aucun `offset`. Un contact au-delà de la limite 500 est introuvable, et la recherche ne porte que sur le texte de la page.

### DI-06 🟠 `PUT`/`DELETE /api/contacts` accessibles avec `clients.voir`
`_MUTATE_PERMS` ne couvre que `POST /api/contacts` (`security.py:150-155`). Pour `PUT` et `DELETE`, la règle de base `("api","contacts") -> clients.voir` s'applique (`security.py:137`). Un profil caissière (`clients.voir` seul) peut donc modifier ou supprimer n'importe quel contact via l'API, malgré l'absence de boutons dans son UI.

### DI-07 🟠 Suppression physique sans contrôle des références
`DELETE FROM contacts WHERE id = ?` (`directory.py:154-160`), sans vérifier les factures, tickets POS, `contact_id` ou l'historique Sage. Le contact disparaît de l'annuaire et du sélecteur POS, et les documents existants gardent un `contact_id` orphelin (ou la suppression échoue si une FK existe, avec un 400 brut).

### DI-08 🟠 Code requis côté serveur, facultatif dans l'UI, immuable ensuite
`create_contact` exige `code` et `nom` (`pos.py:619-621`) ; le formulaire ne valide que `nom` (`clients.html:137`), d'où une erreur générique « Code et nom requis ». En édition, `code` est envoyé (`clients.html:126`) mais absent de la boucle d'`UPDATE` (`directory.py:128-133`) : la modification du code est silencieusement ignorée.

### DI-09 🟡 La page `/clients` exige `clients.gerer` au lieu de `clients.voir`
`(("clients",), "clients.gerer")` (`security.py:136`). La caissière a `clients.voir` mais pas `clients.gerer` : elle ne peut pas consulter l'annuaire, alors qu'elle peut lire/modifier via l'API (DI-06). Incohérence UI/API.

### DI-10 🟡 `/api/contacts/sage` synchrone et non borné
`api_contacts` retourne `fetch_contacts()` directement dans la requête HTTP (`directory.py:26-29`), sans cache, filtre ni pagination. Sur une base Sage volumineuse ou lente, l'appel bloque le worker.

### DI-11 🟡 `limit` non validé sur `/api/contacts`
`min(int(request.args.get("limit",200)),500)` (`directory.py:37`) : `limit=abc` renvoie 500, `limit=-1` donne `LIMIT -1` (aucune limite en SQLite). Même défaut que PO-60.

### DI-12 🟡 Unicité NIU approximative
`create_contact` compare le NIU tel quel (`pos.py:625-636`) ; `update` exclut seulement les NIU vides (`directory.py:119-124`). Les variantes de casse/espaces passent, et le contrôle applicatif n'est pas atomique avec l'insertion (la contrainte `UNIQUE` sur `niu` n'est pas visible dans le schéma).

### DI-13 🟡 Bloc de succès dupliqué dans `saveContact`
Le `if(d.success)` est écrit deux fois (`clients.html:148-149`) : deux toasts et deux timers de rechargement. Cosmétique mais révèle un copier-coller non testé.

### DI-14 🟡 Aucune validation de format NIU/RCCM/email/téléphone
Le formulaire accepte n'importe quelle chaîne (`clients.html:30-48`) et l'API aussi (`pos.py:617-663`). Les données fiscales peuvent être incomplètes ou invalides avant envoi SFEC.

### DI-15 🔵 Toutes les PII sont sérialisées dans la page
`contacts_json` contient NIU, email, téléphone, adresse, RCCM de tous les contacts (`directory.py:197`), même si l'UI n'en affiche qu'une partie. Toute personne ayant accès à la page (ou au cache navigateur) obtient l'annuaire complet.

### DI-16 ⚪ Ergonomie/libellés
Bouton « + Nouveau Client » alors que le type Fournisseur est proposé (`clients.html:10`) ; modale sans fermeture ESC/clic overlay ; bouton Annuler stylé en inline ; accents non systématiques.

---

## 2. Vendeurs (`/vendeurs`)

Fichiers : `app/web/routes/directory.py:59-113`, `app/web/templates/directory/vendeurs.html`, `app/domain/pos.py:724-805`.

### DI-17 🟠 « CA Jour équipe » affiche en réalité le CA total
`get_vendeur_stats()` est appelé sans dates (`directory.py:61`, `pos.py:781-805`) et `ca_jour=sum(s.get("ca_total",0))` (`directory.py:79`). Le libellé « CA Jour equipe » (`vendeurs.html:7`) est donc faux : c'est le cumul historique.

### DI-18 🟠 `GET /api/vendeurs` renvoie le PIN en clair
`list_vendeurs` fait `SELECT *` (`pos.py:774-779`) et la route renvoie toutes les colonnes (`directory.py:88-91`), y compris `pin`. Tout profil `vendeurs.voir` récupère les codes PIN de toute l'équipe dans la réponse réseau.

### DI-19 🔴 PIN stocké en clair
`create_vendeur` insère `data.get("pin","")` sans hachage (`pos.py:732-740`) et `update_vendeur` accepte `pin` brut (`pos.py:756-757`). Si ce PIN devient un jour un secret d'authentification, il est déjà compromis.

### DI-20 🟠 `PUT`/`DELETE /api/vendeurs` accessibles avec `vendeurs.voir`
Comme DI-06, `_MUTATE_PERMS` ne couvre que `POST /api/vendeurs` (`security.py:150-155`). Un utilisateur `vendeurs.voir` peut modifier le rôle, le nom ou désactiver un vendeur via `PUT/DELETE`, voire changer son PIN.

### DI-21 🟡 La page `/vendeurs` exige `vendeurs.gerer`
`(("vendeurs",), "vendeurs.gerer")` (`security.py:138`) alors que la consultation devrait suffire (`vendeurs.voir`). La caissière ne peut pas consulter l'équipe depuis l'UI, mais le peut via l'API.

### DI-22 🟡 Liste plafonnée à 200 et total trompeur
`list_vendeurs(active_only=False)` utilise le `limit=200` par défaut (`pos.py:774-779`) ; `total=len(vendeurs)` (`directory.py:74`). Au-delà de 200 vendeurs, les compteurs et la liste sont incomplets.

### DI-23 🟡 Le code vendeur n'est pas modifiable
`editVendeur` envoie `code` (`vendeurs.html:24`), mais `update_vendeur` n'autorise pas `code` parmi les champs (`pos.py:754-757`). La modification est perdue sans message.

### DI-24 🟡 Aucun contrôle d'unicité ni de format
`create_vendeur` ne vérifie pas le doublon de `code` (`pos.py:724-746`) et n'accepte que `code`/`nom` comme obligatoires. Un doublon lève un `IntegrityError` brut (si contrainte) ou crée deux vendeurs identiques.

### DI-25 🟡 Impossible de réactiver un vendeur depuis l'UI
La liste inclut les inactifs (`active_only=False`) et affiche le badge, mais le formulaire d'édition n'expose pas `est_actif` (`vendeurs.html:13-31`). Seule l'API `PUT {"est_actif":1}` permet de réactiver.

### DI-26 🟡 Le PIN n'est utilisé nulle part pour l'authentification
Recherche dans le code : `pin` n'est lu que par `create_vendeur`/`update_vendeur` (`pos.py:732,756`) et jamais vérifié au login POS. La colonne et les APIs exposent donc un secret inutile.

### DI-27 🟡 Statistiques vendeur sans filtre de date et colonne inutilisée
`get_vendeur_stats` ne filtre que `statut='valide'` et n'applique aucune date (`pos.py:781-805`) ; `panier_moyen` est calculé mais jamais affiché (`directory.py:66-71`). Les colonnes « Ventes » et « CA » sont des cumuls, sans indication.

### DI-28 ⚪ Modale et CSS
`style="...display:none;...display:none;..."` dupliqué (`vendeurs.html:8`), pas de fermeture ESC/overlay, `showNewVendeur` ne remet pas le focus.

---

## 3. Utilisateurs & droits (`/utilisateurs`)

Fichiers : `app/web/routes/directory.py:203-340`, `app/web/templates/directory/utilisateurs.html`, `app/web/routes/sync_api.py:117-154`, `app/web/auth/user_auth.py`.

### DI-29 🟠 Un admin peut se désactiver ou perdre son rôle admin
`api_update_compte` accepte `est_actif` et `role` sans garde (`directory.py:303-320`), et `toggleActif` s'applique à soi-même (`utilisateurs.html:117-123`). Seule la suppression vérifie le dernier admin (`user_auth.py:320-338`) ; un admin unique peut donc se verrouiller dehors.

### DI-30 🟠 Paramètres de sécurité non validés côté serveur
`save_auth_config` copie `max_attempts`, `lock_minutes`, `session_max_age_hours`, etc. sans borne (`user_auth.py:98-110`). Un appel direct à `/api/auth/config` peut mettre 0/negatif et désactiver le verrouillage ou créer une session infinie.

### DI-31 🟠 `https_only` ne s'applique qu'au redémarrage
L'UI l'indique (`utilisateurs.html:22`), mais le paramètre est présenté comme une option de sécurité active. Tant que le service n'est pas redémarré, les cookies restent non sécurisés.

### DI-32 🟡 Le rôle est vidé à la création d'un compte
`visible()` met `u-role.value=""` (`utilisateurs.html:60`) et `showNewUser()` ne restaure pas de rôle par défaut (`utilisateurs.html:61-67`). Si l'utilisateur ne re-sélectionne pas un rôle, `create_user(..., role="")` lève « Role invalide » (`user_auth.py:255-256`).

### DI-33 🟡 Réinitialisation de mot de passe via `prompt()` non masqué
`resetPwd` affiche le mot de passe en clair dans une `prompt` (`utilisateurs.html:107-115`), sans confirmation ni génération aléatoire. Visible par l'entourage et les extensions navigateur.

### DI-34 🟡 Politique de mot de passe minimale
Seule contrainte : 8 caractères (`user_auth.py:251,303`). Pas de complexité, pas de confirmation, pas de changement forcé à la première connexion, pas d'expiration.

### DI-35 🟡 Journal d'audit tronqué et non paginé
`_audit_rows` n'affiche que `entries[:12]` (`directory.py:245-246`) alors que 20 sont chargées (`directory.py:258`). Aucun filtre, export ou pagination.

### DI-36 🟡 IP d'audit souvent vide
`audit()` prend `ip=""` par défaut (`user_auth.py:444-450`) ; la plupart des appels ne fournissent pas l'IP. La colonne IP du journal est donc majoritairement vide, limitant la traçabilité.

### DI-37 🟡 Matrice des droits dupliquée et figée en JS
`saveMatrix` reconstruit les 4 rôles en dur (`utilisateurs.html:131`), alors que la liste vient de `user_auth.ROLES` (`user_auth.py:34`). Tout rôle ajouté côté serveur casse l'éditeur (droits perdus à l'enregistrement).

### DI-38 🟡 `provider` est injecté avec `|safe` et modifiable librement
`{{ provider | safe }}` (`utilisateurs.html:13`) et `save_auth_config` accepte n'importe quelle valeur (`user_auth.py:98-104`), alors que l'UI ne propose que `local`/`sage`. Une valeur HTML injecte du contenu dans la page.

### DI-39 🔵 Mot de passe historique laissé en clair dans la configuration
`migrate_admin_from_config` copie `dashboard.login_password` vers un hash (`user_auth.py:140-160`) mais ne supprime pas la valeur en clair de la configuration. Si la page `/config` l'expose ou si `config.json` fuite, l'ancien mot de passe reste lisible.

### DI-40 🔵 `limit` non validé sur `/api/audit`
`min(int(request.args.get("limit",200)),500)` (`sync_api.py:156`) : 500 sur texte, `-1` contourne la limite. Même famille que DI-11/PO-60.

### DI-41 ⚪ Dette dans le formulaire utilisateur
`visible()` ne réinitialise pas `u-actif` (`utilisateurs.html:60`), le code de remplissage est dupliqué, et la modale n'a pas de fermeture clavier. La fonction `visible` a un nom trompeur (elle vide les champs).

---

## 4. APIs & permissions (transversal)

### DI-42 🔴 `_MUTATE_PERMS` ne couvre que des `POST` exacts
`security.py:149-155` ne surcharge que `POST /api/clients`, `POST /api/contacts`, `POST /api/vendeurs`, `POST /api/products`. Tous les `PUT`/`DELETE` retombent sur la permission de **lecture** de la ressource (`clients.voir`, `vendeurs.voir`), et une URL avec slash final échappe à la correspondance exacte. C'est la cause racine de DI-06 et DI-20 : les droits en écriture ne sont pas appliqués de façon systématique.

### DI-43 🟠 Les erreurs SQL/exception sont renvoyées brutes
`jsonify({"success": False, "error": str(e)})` dans presque toutes les routes (`directory.py:55,142,155,310,329,338`) et `sync_api.py:152`. Chemins SQLite, noms de tables et contraintes fuient vers le client.

### DI-44 🟠 Aucune pagination réelle sur les annuaires
Contacts et vendeurs n'exposent ni `offset` ni recherche serveur (`directory.py:33-39,88-91`) ; le plafond 500/200 est la seule borne. Les grands référentiels Sage sont tronqués silencieusement.

### DI-45 🟡 `total` renvoyé = taille de la page, pas le total
`{"contacts": contacts, "total": len(contacts)}` (`directory.py:38`) et idem vendeurs (`directory.py:91`). Un client qui paginerait ne peut pas connaître le volume réel.

### DI-46 🟡 Pas d'export/import ni de fusion des doublons
Aucun export CSV/PDF de l'annuaire, aucun import, aucune fusion de contacts en doublon. Les corrections se font ligne par ligne, avec un rechargement complet à chaque fois.

### DI-47 🟡 Suppressions non transactionnelles et sans audit métier
Les `DELETE` contacts/vendeurs/utilisateurs ne sont pas enveloppés dans une transaction avec vérification des dépendances (`directory.py:154-160,111-113`). Les suppressions de comptes sont auditées (`user_auth.py:330-337`), pas celles des contacts/vendeurs.

### DI-48 🔵 Aucune synchronisation Sage des contacts/vendeurs
`/api/contacts/sage` lecture seule (`directory.py:26-29`), pas de pull/push planifié ni de résolution de conflits. Les modifications locales et Sage divergent silencieusement.

### DI-49 ⚪ Aucun test dédié à l'annuaire ou aux permissions
Pas de test couvrant les droits `PUT/DELETE`, la duplication NIU, la suppression référencée ou l'échappement HTML. Les régressions de sécurité passent inaperçues.

---

## 5. Suspicions prioritaires à trancher

1. **DI-42/DI-06/DI-20** : vérifier avec un compte `caissiere` qu'un `PUT/DELETE /api/contacts` ou `/api/vendeurs` est bien accepté. Impact : modification/suppression de données sans droit.
2. **DI-01/DI-02** : chercher des contacts Sage contenant `</script>`, `<img`, `&` ; tester le rendu de `/clients`.
3. **DI-19/DI-18** : confirmer si le PIN vendeur est censé authentifier la caisse ; si oui, le stockage en clair est critique.
4. **DI-29/DI-30** : tester l'auto-désactivation du dernier admin et la mise à 0 de `max_attempts` via l'API.
5. **DI-03/DI-44** : compter les contacts réels vs 500 chargés ; vérifier l'impact sur les compteurs et le sélecteur POS.
6. **DI-07/DI-47** : vérifier les références `contact_id` dans `invoices`/`pos_tickets` avant d'autoriser une suppression.
7. **DI-39** : inspecter `config.json` après migration pour confirmer la présence du mot de passe en clair.
