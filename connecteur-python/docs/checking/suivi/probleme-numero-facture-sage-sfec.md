# Problème — Numéro de facture envoyé à SFEC

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/checking/suivi/`
> **Statut** : IDENTIFIÉ / CORRIGÉ — 2026-10-28
> **Sévérité** : CRITIQUE — le bouton « Certifier » de la liste billing ne
> fonctionne plus pour les factures Sage

---

## 1. Constat initial

L'inquiétude portait sur `app/web/templates/billing/list.html` :

```javascript
function certifyBilling(id){
    api("POST","/api/sfec/certify",{invoice_id:"INV-"+id})
}
```

On pensait que SFEC recevait `INV-<id_sqlite>` au lieu du numéro de pièce
Sage (`26FAZ07297`).

## 2. Vérification dans le code

`INV-<id>` est en réalité une **clé de routage interne**. La route
`/api/sfec/certify` résout cet ID vers la ligne SQLite, puis
`sqlite_invoice_to_sfec()` et `db_invoice_to_sfec()` construisent le payload
avec :

```python
"invoice_id": _safe_str(invoice.get("numero"), invoice.get("id", ""))
```

Donc SFEC recevait déjà `26FAZ07297`. Le problème de traçabilité décrit dans
la première version de ce document n'était pas reproduit côté payload.

## 3. Vrai problème constaté

Une modification partielle du frontend a été appliquée :

```javascript
numero_sfec = (t.source === "sage" && t.numero)
    ? t.numero
    : ("INV-" + t.id);
certifyBilling(numero_sfec);
```

Mais `certifyBilling()` ajoutait encore le préfixe :

```javascript
{invoice_id: "INV-" + id}
```

Pour une facture Sage, la requête devenait donc :

```json
{"invoice_id": "INV-26FAZ07297"}
```

La route tentait alors :

```python
int("26FAZ07297")
```

et renvoyait **400 « Format ID invalide »**. Le bouton « Certifier » était
donc cassé pour toutes les factures Sage.

## 4. Conséquences

| Problème | Explication |
|---|---|
| Certification impossible depuis `/billing` | 400 sur les factures Sage |
| Risque de rupture de traçabilité | Si le payload utilisait l'ID interne au lieu du `numero` |
| Contrat front/backend ambigu | Le front envoyait tantôt un ID SQLite, tantôt un numéro Sage |
| Avoirs/retours fragilisés | Le parent doit être référencé par son numéro de pièce Sage |

## 5. Fichiers concernés

- `app/web/templates/billing/list.html` : construction du bouton et payload.
- `app/web/routes/config.py` : route `/api/sfec/certify`.
- `app/integration/sfec/endpoints.py` : construction du payload
  (`invoice_id = numero`).

## 6. Correction attendue

- Le frontend doit envoyer un contrat explicite :
  - `{invoice_id: "<numero Sage>", by: "numero"}` pour une facture Sage ;
  - `{invoice_id: "INV-<id SQLite>"}` pour une facture locale.
- Le backend doit résoudre `by=numero` via `get_invoice_by_numero()`.
- Le contrat historique `INV-<id>` doit rester compatible.
- Le payload SFEC doit toujours utiliser le `numero` stocké en base, jamais
  la clé de routage.

La mise en œuvre détaillée et les tests sont décrits dans
`solution-numero-facture-sage-sfec.md`.
