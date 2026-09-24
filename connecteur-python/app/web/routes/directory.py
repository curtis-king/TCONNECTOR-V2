"""Blueprint directory — Annuaire : /clients /vendeurs /utilisateurs
/api/contacts* /api/vendeurs* /api/utilisateurs*.

Converti depuis app/web/parking/directory.py (étape A6). Corps des fonctions
strictement inchangés ; les routes sont décorées @bp.route avec les mêmes
URL / méthodes que l'ancien app.add_url_rule() de dashboard.py, et le
wrapping _login_required est reproduit à l'identique.
"""

import functools
import json
from flask import Blueprint, jsonify, render_template, request
from app.domain import pos as pos_engine
from app.integration.sage.database import fetch_contacts
from app.storage import db as sqlite_db
from app.web.auth import user_auth
from app.web.common import ALERT_ZONE, _esc, _page, _sidebar, _static_tags
from app.web.auth.security import _current_identity, _get_csrf_token, _login_required

bp = Blueprint("directory", __name__)




@bp.route("/api/contacts/sage")
@_login_required
def api_contacts():
    return jsonify(fetch_contacts())


@bp.route("/api/contacts", methods=["GET"])
@_login_required
def api_list_contacts():
    contacts = pos_engine.list_contacts(
        type_filter=request.args.get("type"),
        search=request.args.get("search"),
        limit=min(int(request.args.get("limit", 200)), 500)
    )
    return jsonify({"contacts": contacts, "total": len(contacts)})


@bp.route("/api/contacts", methods=["POST"])
@_login_required
def api_create_contact():
    data = request.get_json(silent=True) or {}
    try:
        result = pos_engine.create_contact(data)
        if not result.get("success"):
            code = 409 if result.get("field") == "niu" else 400
        else:
            code = 201
        return jsonify(result), code
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route("/vendeurs")
@_login_required
def vendeurs_page():
    vendeurs = pos_engine.list_vendeurs(active_only=False)
    stats = pos_engine.get_vendeur_stats()

    rows = ""
    for v in vendeurs:
        s = "badge-ok" if v.get("est_actif") else "badge-err"
        stxt = "Actif" if v.get("est_actif") else "Inactif"
        vstat = next((x for x in stats if x["id"] == v["id"]), None)
        nb_ventes = vstat["nb_ventes"] if vstat else 0
        ca = vstat["ca_total"] if vstat else 0
        rows += "<tr><td>{}</td><td>{} {}</td><td>{}</td><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.0f}</td><td><span class='badge {}'>{}</span></td><td><button class='btn btn-sm' onclick='editVendeur({})'>Modifier</button></td></tr>".format(
            _esc(v.get("code", "")), _esc(v.get("prenom", "")), _esc(v.get("nom", "")),
            _esc(v.get("role", "")), _esc(v.get("telephone", "") or v.get("email", "") or "-"),
            nb_ventes, ca, s, _esc(stxt), v["id"]
        )

    return _page(render_template("directory/vendeurs.html",
        total=len(vendeurs),
        actifs=sum(1 for v in vendeurs if v.get("est_actif")),
        ca_jour=sum(s.get("ca_total", 0) for s in stats),
        rows=rows
    ))


@bp.route("/api/vendeurs", methods=["GET"])
@_login_required
def api_list_vendeurs():
    vendeurs = pos_engine.list_vendeurs(active_only=False)
    return jsonify({"vendeurs": vendeurs, "total": len(vendeurs)})


@bp.route("/api/vendeurs", methods=["POST"])
@_login_required
def api_create_vendeur():
    data = request.get_json(silent=True) or {}
    return jsonify(pos_engine.create_vendeur(data))


@bp.route("/api/vendeurs/<int:vendeur_id>", methods=["GET"])
@_login_required
def api_get_vendeur(vendeur_id):
    v = pos_engine.get_vendeur(vendeur_id)
    if not v:
        return jsonify({"error": "Non trouve"}), 404
    return jsonify(v)


@bp.route("/api/vendeurs/<int:vendeur_id>", methods=["PUT"])
@_login_required
def api_update_vendeur(vendeur_id):
    data = request.get_json(silent=True) or {}
    return jsonify(pos_engine.update_vendeur(vendeur_id, data))


@bp.route("/api/vendeurs/<int:vendeur_id>", methods=["DELETE"])
@_login_required
def api_delete_vendeur(vendeur_id):
    return jsonify(pos_engine.update_vendeur(vendeur_id, {"est_actif": 0}))


@bp.route("/api/contacts/<int:contact_id>", methods=["PUT"])
@_login_required
def api_update_contact(contact_id):
    data = request.get_json(silent=True) or {}
    if "niu" in data and data["niu"]:
        with sqlite_db.get_cursor() as cur:
            cur.execute("SELECT id FROM contacts WHERE niu = ? AND niu <> '' AND id <> ?",
                        (data["niu"], contact_id))
            if cur.fetchone():
                return jsonify({"success": False,
                                "error": "Doublon NIU '{}'".format(data["niu"]),
                                "field": "niu"}), 409
    try:
        with sqlite_db.get_cursor() as cur:
            fields = []
            vals = []
            for k in ("nom", "type", "email", "telephone", "niu", "adresse", "ville", "pays", "est_actif", "rccm", "is_taxable"):
                if k in data:
                    fields.append("{} = ?".format(k))
                    vals.append(data[k])
            if not fields:
                return jsonify({"success": False, "error": "Aucun champ a modifier"})
            fields.append("updated_at = datetime('now')")
            vals.append(contact_id)
            cur.execute("UPDATE contacts SET {} WHERE id = ?".format(", ".join(fields)), vals)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route("/api/contacts/<int:contact_id>", methods=["DELETE"])
@_login_required
def api_delete_contact(contact_id):
    try:
        with sqlite_db.get_cursor() as cur:
            cur.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route("/clients")
@_login_required
def clients_page():
    contacts = pos_engine.list_contacts(limit=500)
    client_count = 0
    fourni_count = 0
    for c in contacts:
        if c.get("type") == "client":
            client_count += 1
        else:
            fourni_count += 1

    rows = ""
    for c in contacts:
        s = "badge-ok" if c.get("est_actif") else "badge-err"
        stxt = "Actif" if c.get("est_actif") else "Inactif"
        typ = c.get("type", "client")
        tc = "badge-info" if typ == "fournisseur" else "badge-ok"
        niu = c.get("niu", "") or "-"
        rows += """<tr>
            <td>{code}</td><td><span class="badge {tc}">{typ}</span></td>
            <td>{nom}</td><td>{niu}</td><td>{email}</td><td>{tel}</td><td>{ville}</td>
            <td><span class="badge {s}">{stxt}</span></td>
            <td><button class="btn btn-sm btn-primary" onclick="editContact({cid})">Editer</button>
            <button class="btn btn-sm btn-danger" onclick="deleteContact({cid})">Suppr</button></td>
        </tr>""".format(
            code=c.get("code",""), tc=tc, typ=typ.upper(),
            nom=c.get("nom",""), niu=niu, email=c.get("email",""), tel=c.get("telephone",""), ville=c.get("ville",""),
            s=s, stxt=stxt,
            cid=c["id"]
        )

    return render_template("directory/clients.html",
        static_tags=_static_tags(), nav=_sidebar(request.path),
        total=len(contacts), nb_clients=client_count, nb_fournis=fourni_count, nb_total=len(contacts),
        rows=rows,
        contacts_json=json.dumps(contacts),
        csrf_json=json.dumps(_get_csrf_token()),
        toast=ALERT_ZONE
    )


def _user_rows(comptes, me_id):
    rows = ""
    role_cls = {"admin": "badge-ok", "responsable": "badge-info", "caissiere": "badge-warn", "financiere": ""}
    for u in comptes:
        rows += """<tr>
<td>{nom}</td><td>{email}</td>
<td><span class="badge {rc}">{role_label}</span>{me}</td>
<td><span class="badge {sc}">{s}</span></td>
<td>{derniere}</td>
<td>
<button class="btn btn-sm" onclick="editUser({uid})">Editer</button>
<button class="btn btn-sm" onclick="resetPwd({uid})">MdP</button>
<button class="btn btn-sm" onclick="toggleActif({uid})">{tgl}</button>
<button class="btn btn-sm btn-danger" onclick="delUser({uid})">Suppr</button>
</td></tr>""".format(
            nom=_esc("{} {}".format(u.get("prenom", ""), u.get("nom", "")).strip()),
            email=_esc(u["email"]),
            rc=role_cls.get(u["role"], ""), role_label=_esc(u["role_label"]),
            me=' <span class="badge badge-info">vous</span>' if u["id"] == me_id else "",
            sc="badge-ok" if u["est_actif"] else "badge-err",
            s="Actif" if u["est_actif"] else "Inactif",
            derniere=_esc((u.get("derniere_connexion") or "-")[:19]),
            uid=u["id"], tgl="Desactiver" if u["est_actif"] else "Activer",
        )
    return rows


def _matrix_editor(matrix):
    editor = "<table id='matrix'><thead><tr><th>Permission</th>"
    for r in user_auth.ROLES:
        editor += "<th>{}</th>".format(user_auth.ROLE_LABELS[r])
    editor += "</tr></thead><tbody>"
    for perm, label in user_auth.PERMISSIONS.items():
        editor += "<tr><td>{}</td>".format(_esc(label))
        for r in user_auth.ROLES:
            checked = "checked" if perm in (matrix.get(r) or []) else ""
            editor += ('<td style="text-align:center"><input type="checkbox" data-role="{}" '
                       'data-perm="{}" {}></td>').format(r, perm, checked)
        editor += "</tr>"
    return editor + "</tbody></table>"


def _audit_rows(entries):
    rows = ""
    for e in entries[:12]:
        rows += "<tr><td>{}</td><td>{}</td><td><span class='badge badge-info'>{}</span></td><td>{}</td><td>{}</td></tr>".format(
            _esc((e["ts"] or "")[2:16]), _esc(e.get("utilisateur", "")),
            _esc(e.get("action", "")), _esc(e.get("detail", "")), _esc(e.get("ip", "")))
    return rows or '<tr><td colspan="5" style="text-align:center;color:#94a3b8">Aucune activite</td></tr>'


@bp.route("/utilisateurs")
@_login_required
def utilisateurs_page():
    comptes = user_auth.list_users()
    matrix = user_auth.get_matrix()
    audit = user_auth.list_audit(limit=20)
    me = _current_identity() or {}
    acfg = user_auth.get_auth_config()
    role_opts = "".join('<option value="{}">{}</option>'.format(r, user_auth.ROLE_LABELS[r]) for r in user_auth.ROLES)

    return _page(render_template("directory/utilisateurs.html",
        nb=str(len(comptes)),
        activ=str(sum(1 for u in comptes if u["est_actif"])),
        admins=str(sum(1 for u in comptes if u["role"] == "admin" and u["est_actif"])),
        matrix=_matrix_editor(matrix),
        audit=_audit_rows(audit),
        provider=acfg.get("provider", "local"),
        role_opts=role_opts,
    ))


@bp.route("/api/utilisateurs", methods=["GET"])
@_login_required
def api_list_comptes():
    return jsonify({"comptes": user_auth.list_users()})


@bp.route("/api/utilisateurs", methods=["POST"])
@_login_required
def api_create_compte():
    data = request.get_json(silent=True) or {}
    try:
        new_id = user_auth.create_user(
            data.get("nom", ""), data.get("prenom", ""), data.get("email", ""),
            data.get("password", ""), data.get("role", "responsable"))
        return jsonify({"success": True, "id": new_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route("/api/utilisateurs/<int:user_id>", methods=["PUT"])
@_login_required
def api_update_compte(user_id):
    data = request.get_json(silent=True) or {}
    try:
        if data.get("toggle_actif"):
            u = user_auth.get_user(user_id)
            if not u:
                return jsonify({"success": False, "error": "Compte introuvable"}), 404
            user_auth.update_user(user_id, est_actif=not u["est_actif"])
            return jsonify({"success": True})
        user_auth.update_user(
            user_id,
            nom=data.get("nom"), prenom=data.get("prenom"),
            role=data.get("role"), est_actif=data.get("est_actif"))
        if data.get("password"):
            user_auth.set_password(user_id, data["password"],
                                   resetter=_current_identity().get("email", ""))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route("/api/utilisateurs/<int:user_id>", methods=["DELETE"])
@_login_required
def api_delete_compte(user_id):
    try:
        user_auth.delete_user(user_id, acting_user_id=(_current_identity() or {}).get("user_id"))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route("/api/utilisateurs/<int:user_id>/password", methods=["POST"])
@_login_required
def api_reset_compte_password(user_id):
    data = request.get_json(silent=True) or {}
    try:
        user_auth.set_password(user_id, data.get("password", ""),
                               resetter=(_current_identity() or {}).get("email", ""))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400



