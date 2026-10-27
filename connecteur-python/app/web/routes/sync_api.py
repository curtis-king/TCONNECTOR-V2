"""Blueprint sync_api — API sync & divers : /api/sync* /api/connectivity
/api/metrics /api/certified* /api/ledger-accounts /api/retry-queue
/api/auth* /api/audit /api/csrf.

Migré depuis app/web/parking/sync_api.py (étape A4). Les handlers sont
strictement inchangés ; les URL / méthodes HTTP sont identiques aux
app.add_url_rule() historiques de app/web/dashboard.py. Le wrapping @_login_required est reproduit à l'identique, désormais via
un import direct de app.web.auth.security (aucun import circulaire : la
sécurité ne dépend d'aucun module de routes).
"""

import threading
from flask import Blueprint, jsonify, request
from app.integration.sage.database import fetch_certified_invoices, fetch_ledger_accounts
from app.sync import bidirectional as sync_bidirectional
from app.sync.connectivity import get_status
from app.sync.engine import get_cache, get_metrics, get_retry_queue, sync_all
from app.web.auth import user_auth
from app.web.auth.security import _login_required, _apply_session_security, _current_identity, _get_csrf_token, logger


bp = Blueprint('sync_api', __name__)

_sync_guard = threading.Lock()


@bp.route("/api/metrics", methods=["GET"])
@_login_required
def api_metrics():
    return jsonify(get_metrics())


@bp.route("/api/connectivity", methods=["GET"])
@_login_required
def api_connectivity():
    return jsonify(get_status())


@bp.route("/api/sync", methods=["POST"])
@_login_required
def api_sync():
    if not _sync_guard.acquire(blocking=False):
        return jsonify({"ok": False,
                        "error": "Une synchronisation est deja en cours"}), 409

    def _run():
        try:
            sync_all()
        finally:
            _sync_guard.release()

    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        _sync_guard.release()
        return jsonify({"ok": False, "error": "Impossible de lancer la sync"}), 500
    return jsonify({"ok": True, "message": "Sync lancee"})


@bp.route("/api/articles/sync", methods=["POST"])
@_login_required
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


@bp.route("/api/ledger-accounts", methods=["GET"])
@_login_required
def api_ledger_accounts():
    return jsonify(fetch_ledger_accounts())


@bp.route("/api/certified", methods=["GET"])
@_login_required
def api_certified():
    return jsonify(fetch_certified_invoices())


@bp.route("/api/certified/list", methods=["GET"])
@_login_required
def api_certified_list():
    cache = get_cache()
    out = []
    for inv in cache.get("sfec_invoices", []):
        cd = inv.get("certification_date") or ""
        out.append({
            "id": inv.get("id", ""),
            "numero": inv.get("invoice_number") or inv.get("seller_invoice_number", ""),
            "date": cd[:10] if cd else "",
            "statut": (inv.get("invoice_status") or inv.get("certification_status")
                       or inv.get("status") or ""),
            "buyer": inv.get("buyer_name", ""),
            "montant": inv.get("total_ttc", "0"),
            "invoice_type": inv.get("invoice_type", ""),
        })
    if not out:
        # Repli local Sage si SFEC n'a jamais repondu (DB-41).
        try:
            for inv in fetch_certified_invoices():
                out.append({
                    "id": inv.get("id", ""),
                    "numero": inv.get("certif_num", ""),
                    "date": (inv.get("date") or "")[:10],
                    "statut": "CERTIFIE",
                    "buyer": "",
                    "montant": "0",
                    "invoice_type": inv.get("invoice_type", ""),
                })
        except Exception:
            logger.debug("Repli local des factures certifiees indisponible")
    return jsonify({
        "invoices": out,
        "count": len(out),
        "last_sfec": cache.get("last_sfec_sync_at", ""),
    })


@bp.route("/api/retry-queue", methods=["GET"])
@_login_required
def api_retry_queue():
    return jsonify(get_retry_queue())


@bp.route("/api/sync/bi", methods=["POST"])
@_login_required
def api_sync_bi():
    data = request.get_json(silent=True) or {}
    force_all = bool(data.get("force_all", False))
    result = sync_bidirectional.start_manual_sync(force_all=force_all)
    status = 202 if result.get("success") else 409
    return jsonify(result), status


@bp.route("/api/sync/bi/stats", methods=["GET"])
@_login_required
def api_sync_bi_stats():
    return jsonify(sync_bidirectional.get_sync_stats())


@bp.route("/api/auth/permissions", methods=["GET", "POST"])
@_login_required
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


@bp.route("/api/auth/config", methods=["GET", "POST"])
@_login_required
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


@bp.route("/api/csrf", methods=["GET"])
def api_csrf():
    return jsonify({"token": _get_csrf_token()})


@bp.route("/api/audit", methods=["GET"])
@_login_required
def api_audit():
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 500))
    return jsonify({"entries": user_auth.list_audit(limit=limit)})



