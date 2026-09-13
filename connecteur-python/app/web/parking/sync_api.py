"""Parking sync_api — API sync & divers : /api/sync* /api/connectivity /api/metrics /api/certified* /api/auth* /api/audit /api/csrf.

Handlers déplacés mécaniquement depuis app/web/dashboard.py (étape A3).
AUCUN décorateur ici : les routes sont enregistrées dans dashboard.py
via app.add_url_rule(). Corps des fonctions strictement inchangés.
"""

import threading
from app.integration.sage.database import fetch_certified_invoices, fetch_ledger_accounts
from app.sync import bidirectional as sync_bidirectional
from app.sync.connectivity import get_status
from app.sync.engine import get_cache, get_metrics, get_retry_queue, sync_all
from app.web.auth import user_auth
from flask import jsonify, request


def __getattr__(name):
    """Filet de sécurité : délégation vers app.web.dashboard pour tout nom
    non résolu (uniquement effectif sur accès attribut du module). Les noms
    réellement utilisés par les handlers sont liés explicitement en bas de
    ce module (voir section « Liaison dashboard »)."""
    from app.web import dashboard as _dashboard
    return getattr(_dashboard, name)


def api_metrics():
    return jsonify(get_metrics())


def api_connectivity():
    return jsonify(get_status())


def api_sync():
    threading.Thread(target=sync_all, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync lancee"})


def api_articles_sync():
    def _run():
        try:
            from app.sync import article_sync
            result = article_sync.sync_articles_from_sage()
            logger.info("Sync articles API: %s", result)
        except Exception as e:
            logger.error("Sync articles API: %s", e)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync des articles lancee"})


def api_ledger_accounts():
    return jsonify(fetch_ledger_accounts())


def api_certified():
    return jsonify(fetch_certified_invoices())


def api_certified_list():
    cache = get_cache()
    out = []
    for inv in cache.get("sfec_invoices", []):
        cd = inv.get("certification_date") or ""
        out.append({
            "id": inv.get("id", ""),
            "numero": inv.get("invoice_number", ""),
            "date": cd[:10] if cd else "",
            "statut": (inv.get("invoice_status") or inv.get("certification_status")
                       or inv.get("status") or ""),
            "buyer": inv.get("buyer_name", ""),
            "montant": inv.get("total_ttc", "0"),
        })
    return jsonify({
        "invoices": out,
        "count": len(out),
        "last_sfec": cache.get("last_sfec_sync_at", ""),
    })


def api_retry_queue():
    return jsonify(get_retry_queue())


def api_sync_bi():
    try:
        result = sync_bidirectional.full_sync()
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def api_sync_bi_stats():
    return jsonify(sync_bidirectional.get_sync_stats())


def api_auth_permissions():
    if request.method == "GET":
        return jsonify({"roles": user_auth.ROLES, "roles_labels": user_auth.ROLE_LABELS,
                        "permissions": user_auth.PERMISSIONS,
                        "matrix": user_auth.get_matrix(),
                        "defaults": user_auth.DEFAULT_MATRIX})
    data = request.get_json(silent=True) or {}
    try:
        matrix = user_auth.set_matrix(data.get("matrix", {}))
        audit_entry = _current_identity().get("email", "")
        user_auth.audit("droits.modifies", "Matrice des droits mise a jour", email=audit_entry)
        return jsonify({"success": True, "matrix": matrix})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


def api_auth_config():
    if request.method == "GET":
        return jsonify({"config": user_auth.get_auth_config()})
    data = request.get_json(silent=True) or {}
    try:
        cfg = user_auth.save_auth_config(data)
        _apply_session_security()
        login_email = _current_identity() and _current_identity().get("email", "")
        user_auth.audit("securite.modifiee", "Parametres de securite mis a jour", email=login_email or "")
        return jsonify({"success": True, "config": cfg})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


def api_csrf():
    return jsonify({"token": _get_csrf_token()})


def api_audit():
    return jsonify({"entries": user_auth.list_audit(limit=min(int(request.args.get("limit", 200)), 500))})


# ── Liaison dashboard ──
# Ces noms sont définis dans app/web/dashboard.py. On les lie ICI, en bas de
# module : quel que soit l'ordre d'import (dashboard d'abord ou parking
# d'abord), ils existent déjà dans le namespace de dashboard.py à ce stade —
# aucun import circulaire possible. Les corps des fonctions ci-dessus restent
# strictement inchangés (références globales résolues à l'exécution).
from app.web import dashboard as _dashboard
_apply_session_security = _dashboard._apply_session_security
_current_identity = _dashboard._current_identity
_get_csrf_token = _dashboard._get_csrf_token
logger = _dashboard.logger

del _dashboard
