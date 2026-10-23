"""Blueprint sites — Multi-sites impression : /sites /api/sites*.

Configuration legere (data/sites.json) : sites = (users/machine) -> imprimante
A4 (factures Sage) + imprimante ticket 80 mm (POS). Kill-switch global
(pause + dry_run) et enumeration des imprimantes reelles de la machine.
"""
import logging

from flask import Blueprint, jsonify, request

from app.print_hijack import sites as sites_mod
from app.web.auth.security import _login_required, _get_csrf_token
from app.web.common import _esc, _page

logger = logging.getLogger("t-connector.web.sites")

bp = Blueprint("sites", __name__)


@bp.route("/sites", methods=["GET"])
@_login_required
def sites_page():
    data = sites_mod.load_config_dict()
    sw = {"paused": bool(data.get("paused")), "dry_run": bool(data.get("dry_run"))}
    active, reason = sites_mod.resolve_active_site()
    printers = sites_mod.enumerate_printers()

    rows = ""
    for s in data.get("sites", []):
        users = ", ".join(s.get("users") or []) or "-"
        machine = s.get("machine") or "-"
        a4 = s.get("a4_printer") or ""
        ticket = s.get("ticket_printer") or ""
        enabled = bool(s.get("enabled", True))
        a4_ok = sites_mod.printer_connected(a4) if a4 else False
        ticket_ok = sites_mod.printer_connected(ticket) if ticket else False
        badge = '<span class="badge ok">Actif</span>' if enabled else '<span class="badge off">Inactif</span>'
        cur = ' <span class="badge cur">actuel</span>' if active and active.get("id") == s.get("id") else ""
        rows += (
            '<tr data-id="{sid}">'
            '<td><b>{name}</b>{cur}</td>'
            '<td>{users}</td><td>{machine}</td>'
            '<td>{a4} {a4st}</td><td>{ticket} {ticketst}</td>'
            '<td>{badge}</td>'
            '<td class="actions">'
            '<button class="btn btn-sm" onclick="editSite(\'{sid}\')">Editer</button>'
            '<button class="btn btn-sm" onclick="toggleSite(\'{sid}\', {enabled})">'
            '{toggle}</button>'
            '<button class="btn btn-sm btn-danger" onclick="deleteSite(\'{sid}\')">Suppr.</button>'
            '</td></tr>'
        ).format(
            sid=_esc(s.get("id", "")), name=_esc(s.get("name", "")), cur=cur,
            users=_esc(users), machine=_esc(machine),
            a4=_esc(a4),
            a4st='<span class="badge ok">ok</span>' if a4_ok else ('<span class="badge off">absente</span>' if a4 else ""),
            ticket=_esc(ticket),
            ticketst='<span class="badge ok">ok</span>' if ticket_ok else ('<span class="badge off">absente</span>' if ticket else ""),
            badge=badge,
            enabled="true" if enabled else "false",
            toggle="Desactiver" if enabled else "Activer",
        )
    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#64748b">Aucun site - cliquez "Nouveau site"</td></tr>'

    opts = "".join('<option value="{}">{}</option>'.format(_esc(p), _esc(p)) for p in printers)

    pause_cls = "btn-danger" if sw["paused"] else "btn-primary"
    pause_label = "Reprendre les impressions" if sw["paused"] else "Mettre en pause"
    dry_cls = "btn-primary" if not sw["dry_run"] else "btn-danger"
    dry_label = "Desactiver le mode test" if sw["dry_run"] else "Activer le mode test (dry-run)"

    body = """
<div class="card" style="margin-bottom:16px">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
    <div>
      <b>Kill-switch :</b>
      <button id="btn-pause" class="btn {pause_cls}" onclick="togglePause()">{pause_label}</button>
      <button id="btn-dry" class="btn {dry_cls}" onclick="toggleDry()">{dry_label}</button>
    </div>
    <button class="btn btn-primary" onclick="newSite()">+ Nouveau site</button>
  </div>
  <div style="margin-top:8px;font-size:12px;color:#64748b">
    Session : <b>{user}</b> &middot; Machine : <b>{machine}</b> &middot; Site resolu : <b>{active}</b>
    ({reason})
  </div>
</div>

<div class="card">
  <table style="width:100%;border-collapse:collapse">
    <thead><tr style="text-align:left;border-bottom:2px solid #e2e8f0">
      <th style="padding:8px">Site</th><th style="padding:8px">Users</th>
      <th style="padding:8px">Machine</th><th style="padding:8px">Imprimante A4</th>
      <th style="padding:8px">Ticket 80mm</th><th style="padding:8px">Etat</th>
      <th style="padding:8px">Actions</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>

<div class="card" id="site-form" style="display:none;margin-top:16px">
  <h3 id="form-title">Nouveau site</h3>
  <input type="hidden" id="f-id">
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px">
    <label>Nom du site
      <input id="f-name" class="input" placeholder="Ex: Boutique Kinshasa"></label>
    <label>Users Windows (sep. virgule)
      <input id="f-users" class="input" placeholder="Ex: hp, caisse1"></label>
    <label>Machine (hostname, optionnel)
      <input id="f-machine" class="input" placeholder="Ex: PC-BOUTIQUE-01"></label>
    <label>Imprimante A4 (factures Sage)
      <input id="f-a4" class="input" list="printer-list" placeholder="Choisir..."></label>
    <label>Imprimante ticket 80 mm (POS)
      <input id="f-ticket" class="input" list="printer-list" placeholder="Choisir..."></label>
    <label style="display:flex;align-items:center;gap:6px;margin-top:22px">
      <input type="checkbox" id="f-enabled" checked> Site actif</label>
  </div>
  <datalist id="printer-list">{printer_opts}</datalist>
  <div style="margin-top:12px">
    <button class="btn btn-primary" onclick="saveSite()">Enregistrer</button>
    <button class="btn" onclick="hideForm()">Annuler</button>
  </div>
</div>

<script>
var CSRF = "{csrf}";
function hdr(){{ return {{"Content-Type":"application/json","X-CSRF-Token":CSRF}}; }}
function api(url, method, body){{
  return fetch(url, {{method:method||"GET", headers:hdr(), body:body?JSON.stringify(body):undefined}})
    .then(function(r){{ return r.json(); }});
}}
function newSite(){{
  document.getElementById("f-id").value="";
  document.getElementById("f-name").value="";
  document.getElementById("f-users").value="";
  document.getElementById("f-machine").value="";
  document.getElementById("f-a4").value="";
  document.getElementById("f-ticket").value="";
  document.getElementById("f-enabled").checked=true;
  document.getElementById("form-title").textContent="Nouveau site";
  document.getElementById("site-form").style.display="block";
}}
function hideForm(){{ document.getElementById("site-form").style.display="none"; }}
function editSite(id){{
  api("/api/sites").then(function(d){{
    var s=(d.sites||[]).filter(function(x){{return x.id===id;}})[0];
    if(!s)return;
    document.getElementById("f-id").value=s.id||"";
    document.getElementById("f-name").value=s.name||"";
    document.getElementById("f-users").value=(s.users||[]).join(", ");
    document.getElementById("f-machine").value=s.machine||"";
    document.getElementById("f-a4").value=s.a4_printer||"";
    document.getElementById("f-ticket").value=s.ticket_printer||"";
    document.getElementById("f-enabled").checked=s.enabled!==false;
    document.getElementById("form-title").textContent="Editer : "+(s.name||"");
    document.getElementById("site-form").style.display="block";
  }});
}}
function saveSite(){{
  var payload={{
    id: document.getElementById("f-id").value || undefined,
    name: document.getElementById("f-name").value,
    users: document.getElementById("f-users").value.split(",").map(function(s){{return s.trim();}}).filter(Boolean),
    machine: document.getElementById("f-machine").value,
    a4_printer: document.getElementById("f-a4").value,
    ticket_printer: document.getElementById("f-ticket").value,
    enabled: document.getElementById("f-enabled").checked
  }};
  api("/api/sites", "POST", payload).then(function(d){{
    if(d.success){{ location.reload(); }}
    else {{ alert(d.error||"Erreur"); }}
  }});
}}
function toggleSite(id, enabled){{
  api("/api/sites/"+id+"/toggle", "POST").then(function(d){{
    if(d.success) location.reload(); else alert(d.error||"Erreur");
  }});
}}
function deleteSite(id){{
  if(!confirm("Supprimer ce site ?"))return;
  api("/api/sites/"+id, "DELETE").then(function(d){{
    if(d.success) location.reload(); else alert(d.error||"Erreur");
  }});
}}
function togglePause(){{
  api("/api/print/kill-switch", "POST", {{paused: {paused_next}}}).then(function(d){{
    if(d.success) location.reload(); else alert(d.error||"Erreur");
  }});
}}
function toggleDry(){{
  api("/api/print/kill-switch", "POST", {{dry_run: {dry_next}}}).then(function(d){{
    if(d.success) location.reload(); else alert(d.error||"Erreur");
  }});
}}
</script>
""".format(
        pause_cls=pause_cls, pause_label=_esc(pause_label),
        dry_cls=dry_cls, dry_label=_esc(dry_label),
        user=_esc(sites_mod.current_user()), machine=_esc(sites_mod.current_machine()),
        active=_esc((active or {}).get("name", "-")), reason=_esc(reason),
        rows=rows, printer_opts=opts, csrf=_esc(_get_csrf_token()),
        paused_next="false" if sw["paused"] else "true",
        dry_next="false" if sw["dry_run"] else "true",
    )
    return _page(body)


@bp.route("/api/sites", methods=["GET"])
@_login_required
def api_list_sites():
    data = sites_mod.load_config_dict()
    active, reason = sites_mod.resolve_active_site()
    return jsonify({
        "success": True,
        "sites": data.get("sites", []),
        "paused": bool(data.get("paused")),
        "dry_run": bool(data.get("dry_run")),
        "active_site_id": (active or {}).get("id"),
        "active_reason": reason,
        "current_user": sites_mod.current_user(),
        "current_machine": sites_mod.current_machine(),
    })


@bp.route("/api/sites", methods=["POST"])
@_login_required
def api_save_site():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "nom du site requis"}), 400
    users = payload.get("users") or []
    if isinstance(users, str):
        users = [u.strip() for u in users.split(",") if u.strip()]
    site = sites_mod.upsert_site({
        "id": payload.get("id") or "",
        "name": name,
        "users": [str(u).strip() for u in users if str(u).strip()],
        "machine": str(payload.get("machine") or "").strip(),
        "a4_printer": str(payload.get("a4_printer") or "").strip(),
        "ticket_printer": str(payload.get("ticket_printer") or "").strip(),
        "enabled": bool(payload.get("enabled", True)),
    })
    return jsonify({"success": True, "site": site})


@bp.route("/api/sites/<site_id>", methods=["DELETE"])
@_login_required
def api_delete_site(site_id):
    if sites_mod.delete_site(site_id):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "site introuvable"}), 404


@bp.route("/api/sites/<site_id>/toggle", methods=["POST"])
@_login_required
def api_toggle_site(site_id):
    site = sites_mod.get_site(site_id)
    if not site:
        return jsonify({"success": False, "error": "site introuvable"}), 404
    site["enabled"] = not bool(site.get("enabled", True))
    sites_mod.upsert_site(site)
    return jsonify({"success": True, "site": site})


@bp.route("/api/sites/printers", methods=["GET"])
@_login_required
def api_list_printers():
    return jsonify({"printers": sites_mod.enumerate_printers()})


@bp.route("/api/print/kill-switch", methods=["POST"])
@_login_required
def api_kill_switch():
    payload = request.get_json(silent=True) or {}
    result = sites_mod.set_switches(
        paused=payload.get("paused"),
        dry_run=payload.get("dry_run"),
    )
    logger.info("Kill-switch mis a jour: %s", result)
    return jsonify({"success": True, **result})


@bp.route("/api/print/reprocess/<numero>", methods=["POST"])
@_login_required
def api_reprocess_print(numero):
    """Force la reprise d'un PDF bloque en ATTENTE_CERTIF (support)."""
    from app.print_hijack import auto_printer

    cleared = auto_printer.clear_attente_certif(numero)
    logger.info("Reprise impression demandee pour %s (%d ligne(s))", numero, cleared)
    return jsonify({"ok": True, "numero": numero, "cleared": cleared})
