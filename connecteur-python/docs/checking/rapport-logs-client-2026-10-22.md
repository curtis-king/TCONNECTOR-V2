# Rapport d’analyse — logs client T-CONNECTOR SFEC

**Date du rapport :** 27/10/2026  
**Source :** `essai/data-from-customer-2/output.log.1`  
**Période analysée :** 22/10/2026 12:39:41 → 13:34:54, soit 55 minutes  
**Environnement SFEC :** sandbox  
**Statut :** analyse en lecture seule, aucune donnée client modifiée

## 1. Résumé exécutif

Le connecteur tourne, lit Sage et tente de certifier, mais **aucune nouvelle facture n’a été certifiée** pendant la période analysée. Environ **27 cycles** de certification se sont enchaînés avec :

- **0 nouvelle certification** ;
- **6 402 erreurs** ;
- **21 193 avertissements** ;
- **198 factures** distinctes en échec ;
- **529 avoirs/factures négatives** dont la facture d’origine n’est pas certifiée.

Trois causes principales se dégagent :

1. **Le client utilise une version ancienne du connecteur.** Plusieurs bugs visibles dans les logs sont déjà corrigés dans le code actuel.
2. **Le typage des documents Sage est anormal.** Le client a **211 057 documents Type 7** pour seulement **361 documents Type 6**. Le connecteur traite tous les Type 7 comme des avoirs, ce qui est probablement incorrect ou, au minimum, à qualifier avec le système d’auto-avoir de Sage.
3. **Les données fiscales doivent être corrigées.** La majorité des rejets vient du centime additionnel, de la taxe de 3 %, de factures sans TVA et d’avoirs sans facture d’origine certifiée.

## 2. Chiffres clés

| Indicateur | Valeur |
|---|---:|
| Lignes de log | 31 443 |
| Taille du log | 5,2 Mo |
| WARNING | 21 193 |
| ERROR | 6 402 |
| Cycles de certification | ~27 |
| Nouvelles certifications | 0 |
| Factures en échec de certification | 198 |
| Factures avec écart de totaux | 191 |
| Avoirs/factures négatives sans parent certifié | 529 |
| Documents Sage lus par le pull | 211 417 |
| Documents Sage Type 7 | 211 057 |
| Documents Sage Type 6 | 361 |
| Articles Sage lus | 47 322 |
| Articles synchronisés | 0 |
| Réconciliations « CERTIFIE local mais absent SFEC » | 46 |
| Erreurs 409 « facture déjà existante » | 29 |
| Timeouts SFEC | 2 |

## 3. Constat n°1 — version client ancienne

Le client tourne sur un build antérieur aux correctifs du 22/10. Plusieurs symptômes des logs sont déjà corrigés dans le dépôt actuel :

| Symptôme dans les logs client | Correctif déjà présent |
|---|---|
| Sync articles : `Incorrect number of bindings supplied. 12 vs 11` | Correction du binding SQL |
| `SystemError: error return without exception set` sur SQLite | Connexions SQLite thread-local |
| `cannot commit - no transaction is active` | Commit conditionnel |
| `Statuts bloques remis a NULL: 178 factures` à chaque cycle | Ne plus réinitialiser les statuts `ERREUR` |
| Avoirs marqués `ERREUR` au lieu d’attendre le parent | Gestion `parent_pending` |
| 409 « invoice with the same invoice number already exists » | Récupération `DEJA_CERTIFIE` |
| `CERTIFIE` en local mais absent de SFEC | Réconciliation et `A_SURVEILLER` |

**Action prioritaire :** déployer le build actuel chez le client avant toute nouvelle analyse de fond. Sans cette mise à jour, une partie des erreurs observées restera présente même après correction des données.

## 4. Constat n°2 — volume et typage Type 7

### 4.1 Répartition des documents Sage

| Type Sage | Nombre | Interprétation actuelle du connecteur |
|---|---:|---|
| Type 0 | 47 596 | Devis, ignoré |
| Type 1 | 108 | Commande, ignoré |
| Type 3 | 114 | Bon de livraison, ignoré |
| Type 6 | 361 | Facture de vente |
| Type 7 | **211 057** | **Avoir** |
| Achat Type 16 | 28 255 | Achat, ignoré pour la certification |
| Domaine 2 Type 20 | 529 | À qualifier |

Le ratio **211 057 Type 7 pour 361 Type 6** est anormal. Deux hypothèses :

1. Le client a réellement un historique très important d’avoirs, ce qui paraît peu probable à ce niveau.
2. Le numéro **7 ne correspond pas à « avoir »** dans sa version/configuration Sage, ou correspond à un flux d’**auto-avoir** généré automatiquement.

Si le Type 7 est un auto-avoir ou un autre type de document, le connecteur le traite aujourd’hui à tort comme un avoir à certifier.

### 4.2 Impact sur les performances

Le pull lit **211 417 documents** et les met à jour un par un :

```text
Pull Sage: 0 nouvelles, 211417 mises a jour
```

- Le pull prend **6 à 7 minutes**.
- Il est relancé toutes les **30 secondes**.
- Les cycles se chevauchent.
- Le cache de certification est limité aux **1 000 documents** les plus récents.
- Le pull remplit SQLite, mais ne recharge pas le cache moteur avec les 211 000 documents.

Conséquence : les chiffres d’anomalies observés sont **un minimum**, car l’auto-certification ne voit qu’une partie des documents.

### 4.3 Vérifications à faire chez le client

Requête de contrôle des types :

```sql
SELECT DO_Type,
       COUNT(*) AS nb,
       MIN(DO_Date) AS date_min,
       MAX(DO_Date) AS date_max
FROM F_DOCENTETE
WHERE DO_Domaine = 0
GROUP BY DO_Type
ORDER BY DO_Type;
```

Échantillon des Type 7 :

```sql
SELECT TOP 50
       DO_Piece, DO_Date, DO_Ref,
       DO_TotalHT, DO_TotalTTC,
       DO_Statut, DO_Valide, SFEC_STATUT
FROM F_DOCENTETE
WHERE DO_Domaine = 0 AND DO_Type = 7
ORDER BY DO_Date DESC;
```

Compter les Type 7 réellement récents :

```sql
SELECT COUNT(*) AS nb
FROM F_DOCENTETE
WHERE DO_Domaine = 0
  AND DO_Type = 7
  AND cbModification >= DATEADD(day, -7, GETDATE());
```

**Point d’attention :** même avec la fenêtre de 7 jours, si Sage met à jour `cbModification` sur les auto-avoirs, ils peuvent rester dans le pull. Il faut vérifier ce comportement.

## 5. Constat n°3 — certification bloquée

### 5.1 Aucune nouvelle certification

Chaque cycle affiche :

```text
Auto-certif: 842 a comptabiliser, 3 sur SFEC (OK), 197 a certifier
Auto-certification terminee: 0 certifiees, 196 erreurs
```

Le nombre de candidats alterne entre **197** et **19**, avec **196** ou **18** erreurs. Cette alternance est provoquée par des remises à `NULL` de statuts :

```text
Statuts bloques remis a NULL: 178 factures
```

Le connecteur remet les factures en échec dans la file, puis les rejoue au cycle suivant.

### 5.2 Les 191 factures avec écart de totaux

| Cause | Factures | Écart constaté |
|---|---:|---|
| Centime additionnel 5 % de TVA absent | **166** | +0,9 % du HT |
| Taxe 3 % + centime absents | **15** | +3,9 % du HT |
| TVA 18 % absente | **9** | +18 % du HT |
| Arrondi | **1** | 375 FCFA |

#### Centime additionnel — 166 factures

Le centime correspond à **5 % de la TVA**, soit **0,9 % du HT**. Le TTC Sage le contient, mais le payload SFEC ne le ventile pas.

Exemple :

```text
9FAZ00144 : total Sage 21 603 179
            attendu    21 439 656
            écart         163 523 = 0,9 % du HT
```

#### Taxe 3 % + centime — 15 factures

Exemple :

```text
25VFB3827 : total Sage 2 525 037
            attendu    2 444 252
            écart         80 785 = 3 % + 0,9 % du HT
```

La taxe de 3 % ne doit pas être absorbée dans le centime. Elle doit être ventilée séparément avec le code SFEC officiel.

#### TVA 18 % absente — 9 factures

Exemple :

```text
25VFB3807 : total Sage 1 760 000
            attendu    2 076 800
```

Le TTC Sage correspond au HT, sans TVA. C’est une incohérence de données dans Sage.

#### Arrondi — 1 facture

```text
25VFA3134 : écart de 375 FCFA
```

### 5.3 Les 529 avoirs sans parent certifié

Le log contient **529 avoirs/factures négatives** avec **520 références parentes** différentes.

Beaucoup de références ne sont pas des numéros de facture :

- `N°PO2500422`
- `05 375 5025`
- `06 572 12 34`
- `BC N°00258`

Le rapprochement automatique ne peut pas fonctionner dans ces cas. L’ancien build les marque `ERREUR`, puis les remet à `NULL`, puis les rejoue.

Il faut :

- un statut « en attente de parent » distinct de `ERREUR` ;
- un écran de liaison manuelle avoir → facture ;
- une règle qui ne retente pas tant que le parent n’est pas certifié.

### 5.4 Autres rejets

- **23 factures** avec montant global ou ligne à zéro/négatif.
- **29 doublons 409** : la facture existe déjà sur SFEC.
- **46 réconciliations** « CERTIFIE en local mais absent de SFEC ».
- **2 timeouts SFEC** de 30 secondes.

## 6. Constat n°4 — robustesse

- **Sync articles :** 47 322 articles lus, **0 inséré**, à cause du binding SQL. Le catalogue n’est pas à jour.
- **SQLite :** 3 exceptions `/login` et 6 erreurs de sync dues à la connexion partagée entre threads.
- **Redémarrage :** deux démarrages (12:39 et 13:29) et un trou de 14 minutes. Le process a probablement été arrêté/redémarré ou a bloqué pendant un pull de 211 000 documents.
- **Sandbox :** 79 appels vers `sandbox.api.sfec.gouv.cg`. À confirmer si c’est volontaire.
- **Volume de logs :** 5,2 Mo en 55 minutes, dominés par les mêmes erreurs répétées.

## 7. Actions recommandées

### 7.1 Côté client

1. **Mettre à jour le connecteur** avec le build actuel.
2. **Vérifier la signification de `DO_Type = 7`** dans sa version Sage.
3. **Qualifier le système d’auto-avoir** : les Type 7 sont-ils des avoirs réels, des auto-avoirs, ou un autre flux ?
4. **Fournir le résultat des requêtes de diagnostic** ci-dessus.
5. **Corriger les 9 factures sans TVA** dans Sage.
6. **Confirmer les codes de taxe** utilisés pour le centime et la taxe de 3 %.
7. **Confirmer l’environnement SFEC** : sandbox ou production.
8. **Valider les 529 avoirs sans parent** : liaison manuelle, exclusion, ou correction de la référence.

### 7.2 Côté connecteur

1. **Fenêtre de 7 jours** : déjà implémentée, à déployer et vérifier avec les auto-avoirs.
2. **Full sync asynchrone** : tâche de fond, pagination par lots, progression, pas de timeout HTTP.
3. **Single-flight** : un seul full sync à la fois.
4. **Mapping Type 7 configurable** : ne plus supposer que 7 = avoir pour tous les clients.
5. **Ventilation centime / 3 %** : deux taxes distinctes, codes SFEC dédiés.
6. **Avoirs en attente de parent** : statut dédié, liaison manuelle, pas de retry infini.
7. **409 / réconciliation** : marquage `DEJA_CERTIFIE` fiable et reprise.
8. **Observabilité** : version, identifiant de cycle, compteurs, alerte si 0 certification sur X cycles.
9. **Tests de charge** : jeu de 211 000 documents, full sync concurrent, pagination.

### 7.3 Priorités

| Priorité | Action | Responsable |
|---|---|---|
| P0 | Déployer le build actuel | Client / intégrateur |
| P0 | Vérifier Type 7 et auto-avoirs | Client |
| P0 | Fournir les résultats des requêtes de diagnostic | Client |
| P1 | Full sync async + pagination + single-flight | Connecteur |
| P1 | Ventilation centime / 3 % | Connecteur |
| P1 | Avoirs en attente de parent | Connecteur |
| P2 | Observabilité et tests de charge | Connecteur |

## 8. Informations demandées au client

- Version exacte du connecteur actuellement installée.
- Résultat de la requête de répartition des `DO_Type`.
- Échantillon de 20 à 50 documents Type 7 récents.
- Confirmation de la signification des Type 7 et du système d’auto-avoir.
- Codes de taxe Sage utilisés pour le centime et la taxe de 3 %.
- Confirmation sandbox / production.
- Liste ou export des 529 avoirs sans parent, si disponible.
- Validation des 9 factures sans TVA.

## 9. Annexes

### 9.1 Preuves horodatées

| Heure | Événement |
|---|---|
| 12:39:44 | Sync articles : 47 322 lus, 0 inséré |
| 12:39:57 | 211 417 documents Sage lus |
| 12:40:22 | Sync initiale : 1 000 ventes, 1 000 achats |
| 12:40:35 | 197 factures à certifier |
| 12:40:39 | 0 certifiée, 196 erreurs |
| 12:42:50 | Pull : 211 417 mises à jour |
| 12:47:04 | 178 statuts remis à `NULL` |
| 13:09:16 | Pull : 211 417 mises à jour |
| 13:15:36 | Fin du pull après ~6 minutes |
| 13:29:21 | Redémarrage du connecteur |
| 13:34:54 | Dernière erreur de certification |

### 9.2 Factures sans TVA — 9 documents

```text
25VFB3807   1 760 000 → 2 076 800
25VFA3126   1 000 000 → 1 180 000
25VFB3817     400 745 →   472 879,10
25VFA3118     250 000 →   295 000
25VFA3136     150 000 →   177 000
25VFA3133     150 000 →   177 000
25VFA3117     150 000 →   177 000
9FAZ00138      11 719 →    13 828,42
25VFA3140       5 467 →     6 451,06
```

### 9.3 Factures 3 % + centime — 15 documents

```text
25VFB3827   2 525 037 → 2 444 252
25VFB3805   2 525 037 → 2 444 252
25VFB3806     817 501 →   791 345,76
25VFB3826     719 440 →   696 423,02
25VFB3825     719 440 →   696 423,02
25VFA3089     700 000 →   677 604,38
25VFA3090     559 500 →   541 599,94
25VFA3135     395 477 →   382 823,86
25VFA3129      14 628 →    14 160
25VFA3120      14 628 →    14 160
25VFA3119      14 628 →    14 160
25VFA3123       4 788 →     4 635,04
25VFA3122       4 788 →     4 635,04
25VFA3121       4 788 →     4 635,04
9FAZ00139       3 714 →     3 595,46
```

### 9.4 Top 20 des factures avec centime manquant

```text
9FAZ00144  21 603 179 → 21 439 656
25VFB3831  10 913 672 → 10 831 061,88
25VFB3830  10 913 672 → 10 831 061,88
25VFB3828  10 913 672 → 10 831 061,88
25FAZ1983   9 900 000 →  9 825 063,50
25VFR0136  -9 701 041 → -9 627 610,56
25VFB3829   9 701 041 →  9 627 610,56
25VFA3142   8 909 513 →  8 842 072,76
9FAZ00118   8 900 000 →  8 832 632,76
25FRZ0032  -8 900 000 → -8 832 632,76
9FAZ00104   6 078 269 →  6 032 259,12
9FAZ00132   1 713 561 →  1 700 590,04
25VFB3808     863 994 →    857 454,08
9FAZ00102     854 776 →    848 305,54
9FAZ00057     842 432 →    836 054,78
9FAZ00098     833 300 →    826 992,38
9FAZ00119     821 230 →    815 014,20
25FRZ0035    -821 230 →   -815 014,20
9FAZ00129     819 327 →    813 125,02
25VFA3107     814 156 →    807 993,20
```

### 9.5 Limites de l’analyse

- Les logs sont horodatés du **22/10**, pas du 27/10.
- Aucun accès à la base Sage du client : l’analyse repose uniquement sur les logs.
- Le cache de certification ne contient que **1 000 documents** : les 198 anomalies sont donc un **minimum**.
- L’environnement est **sandbox** : le comportement en production peut différer.
- Aucune donnée client n’a été modifiée.
- La tolérance de validation n’a pas été relâchée.
