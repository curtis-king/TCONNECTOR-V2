# Correctifs restants — procédures (inspection pages 2026-09-23)

> Copie de référence : `C:\Users\tp_m2\Documents\projects\TCONNECTOR-V2\connecteur-python` (celle qui tourne).
> Redémarrer le serveur Windows après chaque fichier `.py` touché (processus garde l'ancien code en mémoire).
> **Déjà corrigés** (rappel, rien à faire) : `api_create_contact` + doublon `GET /api/contacts` (`directory.py:25-52`),
> dropdown avoir `billing.py:208` (ligne supprimée), page Utilisateurs (`{rows}` + `rows=_user_rows`),
> `print.html:32` (`invoice_type == "creditNote"`).
> **R11 (export/import) à traiter EN DERNIER**, comme demandé.

---

## R1 — XSS stocké : lignes clients (`directory.py:180-191`)

### Diagnostic
`clients_page` construit `rows` par `.format()` avec valeurs brutes (`code/nom/niu/email/tel/ville`).
Tout contact malveillant s'exécute chez chaque lecteur de `/clients`.

### Bloc à remplacer (`directory.py:186-191`)
```python
        """.format(
            code=c.get("code",""), tc=tc, typ=typ.upper(),
            nom=c.get("nom",""), niu=niu, email=c.get("email",""), tel=c.get("telephone",""), ville=c.get("ville",""),
            s=s, stxt=stxt,
            cid=c["id"]
        )
```

### Bloc corrigé
```python
        """.format(
            code=_esc(c.get("code","")), tc=tc, typ=_esc(typ.upper()),
            nom=_esc(c.get("nom","")), niu=_esc(niu), email=_esc(c.get("email","")),
            tel=_esc(c.get("telephone","")), ville=_esc(c.get("ville","")),
            s=s, stxt=_esc(stxt),
            cid=int(c["id"])
        )
```

### Validation
`py_compile` → restart → `/clients` identique visuellement ; injecter `<img src=x onerror=alert(1)>` dans un nom → rendu inerte (texte affiché).

---

## R2 — XSS DOM : `filterSales` (`directory/sales.html:39-57`)

### Diagnostic
Ligne 53 concatène `t.numero/tiers_nom/mode_paiement/vendeur` sans échappement avant `innerHTML`.

### Bloc à remplacer (`sales.html`, avant `function filterSales(){`)
```javascript
function filterSales(){
```

### Bloc corrigé
```javascript
function escS(s){return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
function filterSales(){
```

### Puis remplacer la construction de ligne (`sales.html:53`)
```javascript
      html+="<tr><td>"+t.numero+"</td><td>"+(t.date_ticket||"").substring(0,16)+"</td><td>"+t.tiers_nom+"</td><td style='text-align:right'>"+Number(t.montant_ttc).toLocaleString()+"</td><td>"+t.mode_paiement+"</td><td>"+(v.trim()||"-")+"</td><td>"+sfecCell(t)+"</td><td><a href='/pos/ticket/"+t.id+"/print' class='btn btn-sm' target='_blank'>Voir</a></td></tr>"
```

### Par
```javascript
      html+="<tr><td>"+escS(t.numero)+"</td><td>"+escS((t.date_ticket||"").substring(0,16))+"</td><td>"+escS(t.tiers_nom)+"</td><td style='text-align:right'>"+Number(t.montant_ttc).toLocaleString()+"</td><td>"+escS(t.mode_paiement)+"</td><td>"+escS(v.trim()||"-")+"</td><td>"+sfecCell(t)+"</td><td><a href='/pos/ticket/"+encodeURIComponent(t.id)+"/print' class='btn btn-sm' target='_blank'>Voir</a></td></tr>"
```

### Validation
Filtre `/sales` fonctionnel ; `sfecCell` déjà sûr (badges + `certifySales('+ic+')` numérique).

---

## R3 — `deny.html` : `|safe` sur données compte (`auth/deny.html:9`)

### Diagnostic
`<b>{{ label | safe }}</b> ... {{ email | safe }}` — sûr seulement si la vue échappe avant.

### Procédure
1. `grep -rn "deny.html" app/web/routes/` → repérer la vue.
2. Dans cette vue, envelopper : `label=_esc(label)`, `email=_esc(email)` avant `render_template`.
3. Alternative (si la vue passe déjà de l'échappé) : passer à `{{ label }}` / `{{ email }}` sans `|safe`.

### Validation
`/deny` (ou route 403) : source HTML sans `<script>` injecté via email.

---

## R4 — Gate admin : aucun contrôle de rôle (systémique)

### Diagnostic
Tout est sous `@_login_required` (`app/web/auth/security.py:98`), aucun check de rôle :
gestion utilisateurs/vendeurs (`directory.py`), matrice permissions + config auth + audit (`sync_api.py:117-156`),
import/export/reload config (`config.py`). Un compte `caissiere` (`ROLES = ["admin","responsable","caissiere","financiere"]`, `user_auth.py:34`) peut tout administrer.

### Procédure
1. Dans `app/web/auth/security.py` (après `_login_required`, ~l.112), ajouter :
```python
def _admin_required(f):
    @_login_required
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        me = _current_identity() or {}
        try:
            u = user_auth.get_user(me.get("user_id"))
        except Exception:
            u = None
        if not u or u.get("role") != "admin":
            return render_template("auth/deny.html",
                label=_esc("administration"), email=_esc(me.get("email", ""))), 403
        return f(*args, **kwargs)
    return decorated
```
(`functools`, `render_template`, `_esc` déjà importés/usités dans le module : `security.py:12,20-23` + `_esc` défini localement.)
2. Remplacer `@_login_required` par `@_admin_required` sur : `directory.py` (`/utilisateurs`, `/api/utilisateurs*`, `/api/vendeurs*` en écriture),
`sync_api.py:117-148` (permissions/config/audit), `config.py` (import/export/reload + `POST /api/config/*`).
3. Laisser `@_login_required` sur toutes les routes de lecture/exploitation (caisse, facturation, config lecture).

### Validation
Connecté `caissiere` → `POST /api/utilisateurs` = 403 + page `deny` ; `admin` = 200. `pytest tests/` vert.

---

## R5 — Notifications jamais sauvegardées (`config.py:436-438` + `index.html:134-138`)

### Diagnostic (double faute)
- `saveForm` envoie `FormData` : case décochée = **absente** ; case cochée = `value="invoice:synced"`.
- Backend : `if form_key in data and _bool(data[form_key])` — `_bool("invoice:synced") == False` → même cochée, jamais enregistrée.

### Bloc à remplacer (`config.py:436-438`)
```python
    for form_key, event_name in event_keys.items():
        if form_key in data and _bool(data[form_key]):
            events.append(event_name)
```

### Bloc corrigé (présence = coché, sémantique FormData)
```python
    for form_key, event_name in event_keys.items():
        if form_key in data:
            events.append(event_name)
```

### Validation
Cocher 2 événements → Sauvegarder → recharger `/config` : cases toujours cochées ; `GET /api/config` → `notifications.events` contient les 2.

---

## R6 — Formulaire facturation : édition qui réinitialise (`billing/form.html` + `billing.py`)

### Diagnostic
- `payment_method` (`form.html:29-35`) / `devise` (`:39-43`, `XAF selected` en dur) : la valeur courante n'est jamais injectée → l'édition réaffiche Virement/XAF.
- `is_recipient_taxable` (`:191-194`) : `Oui` par défaut, valeur courante jamais passée.
- `products_json` jamais injecté dans `<datalist id="products-list">` (`:376`) → datalist morte.
- `contacts_json/products_json/...|safe` sans `|tojson` → `</script>` dans un nom casse le JS.

### Procédure
1. `billing.py::_invoice_form_page` (~l.187-190, après `payment_method`/`devise`) : ajouter
```python
    pm_opts = "".join(
        '<option value="{}"{}>{}</option>'.format(v, " selected" if payment_method == v else "", lbl)
        for v, lbl in (("bank_transfer","Virement"),("cash","Especes"),("card","Carte"),
                       ("mobile_money","Mobile Money"),("cheque","Cheque"),("credit","Credit")))
    dev_opts = "".join(
        '<option value="{}"{}>{}</option>'.format(v, " selected" if devise == v else "", v)
        for v in ("XAF","USD","EUR"))
    tax_oui = "selected" if str(inv.get("is_recipient_taxable", 1) if is_edit else 1) == "1" else ""
    tax_non = "" if tax_oui else "selected"
```
et passer `pm_opts=pm_opts, dev_opts=dev_opts, tax_oui=tax_oui, tax_non=tax_non` au `render_template`.
2. `form.html:29-35` → `<select name="payment_method">{{ pm_opts | safe }}</select>` ; `:39-43` → `<select name="devise">{{ dev_opts | safe }}</select>` ;
`:191-194` → `<option value="1" {{ tax_oui }}>Oui</option><option value="0" {{ tax_non }}>Non</option>`.
3. Peupler la datalist : après `taxRates_json` (~l.280-284), ajouter un `initProductsList()` qui remplit `#products-list` depuis `products_json` (désignation + ref).
4. Remplacer `{{ contacts_json|safe }}` (et `products_json`, `tax_rates_json`, `lignes_json`, `certified_sales_json`) par `|tojson` côté template.

### Validation
Éditer une facture `Especes/USD/Non assujetti` → champs pré-sélectionnés ; datalist propose les articles ; nom avec `</script>` → pas de casse JS.

---

## R7 — Crash `None` : `billing.py:133`, `pos.py:93`

### Blocs à remplacer
```python
# billing.py:133
sfec_html = '<tr><td>SFEC</td><td><span class="badge {}">{}</span></td></tr><tr><td>N Certif</td><td>{}</td></tr>'.format(sfb, _esc(ss), _esc(inv.get("sfec_num_certif", "")[:30] or "-"))
# pos.py:93
num=_esc(t.get("numero", "")), time=_esc(t.get("date_ticket", "")[11:19] or ""),
```

### Blocs corrigés
```python
# billing.py:133 — le [:30] doit porter sur (valeur or "")
sfec_html = '<tr><td>SFEC</td><td><span class="badge {}">{}</span></td></tr><tr><td>N Certif</td><td>{}</td></tr>'.format(sfb, _esc(ss), _esc((inv.get("sfec_num_certif") or "")[:30] or "-"))
# pos.py:93
num=_esc(t.get("numero", "")), time=_esc((t.get("date_ticket") or "")[11:19] or ""),
```

### Validation
`py_compile` → restart → `/billing/invoice/<id>` avec `sfec_num_certif=NULL` et `/pos` avec ticket sans date → 200, pas 500.

---

## R8 — `?limit=abc` → 500 (5 spots)

### Diagnostic
`int(request.args.get(...))` sans garde : `billing.py:247`, `directory.py:37`, `pos.py:30,271`, `sync_api.py:159`.
(Référence saine existante : `billing.py:83` avec `type=int`.)

### Procédure (identique, 5 fois) — exemple `directory.py:37`
Remplacer :
```python
        limit=min(int(request.args.get("limit", 200)), 500)
```
Par :
```python
        limit=min(request.args.get("limit", 200, type=int) or 200, 500)
```
Appliquer le même motif aux 4 autres spots (`billing.py:247-248` `limit`/`offset`, `pos.py:30`, `pos.py:271`, `sync_api.py:159`).

### Validation
`GET /api/contacts?limit=abc` → 200 (limite par défaut), plus 500.

---

## R9 — SFEC : faux 404 + passthrough sans filet (`config.py:191,218,237`)

### Diagnostic
- `:218,237` : `if inv["id"] == invoice_id` — `id` int du cache vs `invoice_id` str du JSON → jamais égal → 404 fantôme.
- `:191` : `return jsonify(certify_single(invoice_id))` hors `try` → toute erreur Sage/SFEC = 500 HTML au lieu de JSON.

### Blocs à remplacer
```python
    return jsonify(certify_single(invoice_id))
```
```python
        if inv["id"] == invoice_id:
```

### Blocs corrigés
```python
    try:
        return jsonify(certify_single(invoice_id))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
```
```python
        if str(inv.get("id")) == str(invoice_id):
```
(2 occurrences : `:218` debug et `:237` validate.)

### Validation
Certifier depuis `/pending` un id Sage brut (non `INV-`) → JSON propre même en échec ; debug/validate retrouvent la facture.

---

## R10 — POS : `fetch()` sans CSRF (`pos/index.html:223,246`)

### Diagnostic
`scanBarcode` (`:223`) et `posInstantSearch` (`:246`) appellent `fetch()` brut → pas de `X-CSRF-Token` → 403 si CSRF strict (le helper global `api()` de `app.js:91` gère token + retry).

### Procédure
Remplacer :
```javascript
    fetch("/api/products/barcode?code="+encodeURIComponent(code)).then(function(r){return r.json()}).then(function(d){
```
Par :
```javascript
    api("GET","/api/products/barcode?code="+encodeURIComponent(code)).then(function(d){
```
Et :
```javascript
    fetch("/api/pos/products/search?q="+encodeURIComponent(raw)).then(function(r){return r.json()}).then(function(list){
```
Par :
```javascript
    api("GET","/api/pos/products/search?q="+encodeURIComponent(raw)).then(function(list){
```
(supprimer le `.then(r=>r.json())` intermédiaire : `api()` résout déjà le JSON.)

### Validation
Scan code-barres + recherche article OK même avec CSRF strict ; erreurs réseau → toast (déjà géré par `api()`).

---

## R11 — [EN DERNIER] Export secrets + import destructif (`config.py:493-539`)

### Diagnostic
- Export (`:493-512`) : `json.dump(cfg)` brut → `db.password`, `sfec.api_key`, `dashboard.login_password/session_secret` en clair à tout authentifié (alors que `GET /api/config` masque déjà password/api_key, `:284-290`).
- Import (`:515-539`) : `save_config(data)` remplace tout le fichier → sections optionnelles absentes (`queue/validation/notifications/rate_limit/log_level`) effacées ; un export masqué réimporté écraserait les secrets par `"***"`.

### Procédure
1. Extraire le masquage en helper (au-dessus de `api_get_config`) :
```python
def _masked_config():
    cfg = get_config()
    masked = dict(cfg)
    for section in ("db", "sfec", "dashboard"):
        if section in masked and isinstance(masked[section], dict):
            masked[section] = dict(masked[section])
    for key in ("password",):
        if key in masked.get("db", {}):
            masked["db"][key] = "***"
    for key in ("api_key",):
        if key in masked.get("sfec", {}):
            masked["sfec"][key] = "***"
    for key in ("login_password", "session_secret"):
        if key in masked.get("dashboard", {}):
            masked["dashboard"][key] = "***"
    return masked
```
`api_get_config` (`:282-291`) devient `return jsonify(_masked_config())`.
2. Export (`:498-500`) : `json.dump(_masked_config(), tmp, ...)` au lieu de `cfg`.
3. Import (`:535-536`) : merge + garde `"***"` + backup :
```python
    try:
        import shutil, time
        shutil.copy(config_path(), config_path() + ".bak-{}".format(int(time.time())))
        cfg = get_config()
        for section, values in data.items():
            if isinstance(values, dict):
                merged = dict(cfg.get(section, {}))
                for k, v in values.items():
                    if v == "***":
                        continue
                    merged[k] = v
                cfg[section] = merged
            else:
                cfg[section] = values
        save_config(cfg)
        return jsonify({"ok": True, "message": "Configuration importee avec succes"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
```
(`config_path()` : vérifier le nom exact dans `app/config/manager.py` — `grep -n "def config_path\|CONFIG_PATH" app/config/manager.py` ; adapter si besoin.)

### Validation
Export → `grep` du fichier téléchargé : 0 occurrence des secrets ; réimport → `GET /api/config` : secrets effectifs inchangés, sections optionnelles intactes, `.bak-*` créé.

---

## R12 — Mineurs (présentation-safe, dette)

| # | Cible | Procédure |
|---|---|---|
| a | `certified.html` colspan | lignes message vide/erreur : `colspan="6"` → `"7"` (7 colonnes avec Type) |
| b | Pastille polling | `dashboard/index.html:19` : conditionner sur `db.polling_enabled` passé par `dashboard.py` au lieu du `<span>Actif</span>` en dur |
| c | PDF `RemiseApres` | `pdf.py:240` : `"Apres remise"` → `"Net HT"` ; `:259` colWidths `16,18` → `14,20` (prendre 2mm à Designation `40→38`, total 172mm inchangé) |
| d | PDF quantité | `pdf.py:249` : `"{:.2f}"` → `"{:,.0f}"` (choix : uniformité 0 décimale) |
| e | Placeholders société | `config.json company` : renseigner `name/address/tax_number/rc_number/legal_form/tax_regime/capital/bank_*` (code 7.8 déjà OK, ce sont les valeurs) |
| f | `GET /logout` | `auth.py:82` : passer en POST (formulaire) — CSRF logout sinon |
| g | Label vendeurs | `directory/vendeurs.html:6` + `vendeurs_page` : `ca_jour` = somme historique → renommer "CA total équipe" ou filtrer jour |
| h | `import functools` mort, `send_file` réimporté, année pied login | nettoyage style, zéro risque |

---

## Validation globale (après chaque R appliqué)

```bash
cd C:\Users\tp_m2\Documents\projects\TCONNECTOR-V2\connecteur-python
python -m py_compile app/web/routes/directory.py app/web/routes/billing.py app/web/routes/config.py app/web/routes/pos.py app/web/routes/sync_api.py app/web/auth/security.py
pytest tests/   # non-régression (dont test_contact_doublon_niu_rejete, snapshots)
# restart serveur → rejouer les gates de chaque R
```
