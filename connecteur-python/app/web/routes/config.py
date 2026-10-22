"""Blueprint config — Configuration : /config /api/config* /api/db/* /api/tables
/api/sfec/* /api/tax-rates*.

Migré depuis app/web/parking/config.py (étape A5). Les handlers sont
strictement inchangés ; les URL / méthodes HTTP sont identiques aux
app.add_url_rule() historiques de app/web/dashboard.py. Le wrapping @_login_required est reproduit à l'identique, désormais via
un import direct de app.web.auth.security (aucun import circulaire : la
sécurité ne dépend d'aucun module de routes).
"""

import copy
import functools
import json
import os
import tempfile
import threading
from flask import Blueprint, after_this_request, jsonify, render_template, request, send_file
from app.config.manager import get_config, save_config
from app.domain import invoices as invoice_engine, pos as pos_engine
from app.storage import db as sqlite_db
from app.integration.sage.database import fetch_tax_rates, list_all_tables, mark_to_monitor, ping_database
from app.integration.sfec.client import SfecClient
from app.integration.sfec.endpoints import certify_sqlite_invoice, check_health, preview_sfec_payload, validate_sfec_payload
from app.sync.engine import apply_config, certify_single, get_cache, sync_sfec_invoices
from app.web.auth import user_auth
from app.web.auth.security import _login_required, _current_identity
from app.web.common import _esc, _page


bp = Blueprint('config', __name__)

_SECRET_KEYS = {
    "password", "api_key", "session_secret", "login_password",
    "secret", "token", "private_key",
}


def _redact_config(value):
    """Copie récursive d'une config avec tous les secrets masqués."""
    if isinstance(value, dict):
        return {
            key: ("***" if key.lower() in _SECRET_KEYS and val not in (None, "")
                  else _redact_config(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    return value


def _merge_import_config(base, incoming):
    """Fusionne un import sans écraser un secret par le masque '***'."""
    result = copy.deepcopy(base)
    for key, val in incoming.items():
        if key.lower() in _SECRET_KEYS and val == "***":
            continue
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_import_config(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result



@bp.route("/config", methods=["GET"])
@_login_required
def config_page():
    cfg = get_config()
    db = cfg.get("db", {})
    sfec = cfg.get("sfec", {})
    company = cfg.get("company", {})
    dash = cfg.get("dashboard", {})
    queue = cfg.get("queue", {})
    validation = cfg.get("validation", {})
    notifications = cfg.get("notifications", {})
    rate_limit = cfg.get("rate_limit", {})
    log_level = cfg.get("log_level", "info")

    def _s(val, target):
        return "selected" if val == target else ""

    def _ck(val):
        return "checked" if val else ""

    # ── TAB 5: VALIDATION & LIMITES ──
    notif_events = notifications.get("events", [])
    body = render_template("config/index.html",
        legal_form=_esc(company.get("legal_form", "")),
        rc_number=_esc(company.get("rc_number", "")),
        capital=_esc(str(company.get("capital", "") or "")),
        logo_file_name=_esc(company.get("logo_file_name", "")),
        server=_esc(db.get("server", "")),
        port=_esc(db.get("port", 1433)),
        database=_esc(db.get("database", "")),
        user=_esc(db.get("user", "")),
        password="",
        tc_f=_s(db.get("trusted_connection"), False),
        tc_t=_s(db.get("trusted_connection"), True),
        enc_f=_s(db.get("encrypt"), False),
        enc_t=_s(db.get("encrypt"), True),
        tsc_t=_s(db.get("trust_server_certificate"), True),
        tsc_f=_s(db.get("trust_server_certificate"), False),
        pool_max=db.get("pool_max", 10),
        pool_min=db.get("pool_min", 2),
        pool_idle_timeout_ms=db.get("pool_idle_timeout_ms", 30000),
        connection_timeout_ms=db.get("connection_timeout_ms", 15000),
        request_timeout_ms=db.get("request_timeout_ms", 30000),
        api_key="",
        sb_t=_s(sfec.get("use_sandbox"), True),
        sb_f=_s(sfec.get("use_sandbox"), False),
        en_t=_s(sfec.get("enabled"), True),
        en_f=_s(sfec.get("enabled"), False),
        cs_sa=_s(sfec.get("certify_status"), "SAISI"),
        cs_co=_s(sfec.get("certify_status"), "CONFIRME"),
        cs_ac=_s(sfec.get("certify_status"), "A COMPTABILISER"),
        cs_c=_s(sfec.get("certify_status"), "COMPTABILISE"),
        cs_l=_s(sfec.get("certify_status"), "LETTRÉ"),
        base_url=_esc(sfec.get("base_url", "")),
        sandbox_url=_esc(sfec.get("sandbox_url", "")),
        name=_esc(company.get("name", "")),
        tax_number=_esc(company.get("tax_number", "")),
        address=_esc(company.get("address", "")),
        city=_esc(company.get("city", "")),
        country=_esc(company.get("country", "")),
        phone=_esc(company.get("phone", "")),
        email=_esc(company.get("email", "")),
        cur_xaf=_s(company.get("currency"), "XAF"),
        cur_usd=_s(company.get("currency"), "USD"),
        bank_name=_esc(company.get("bank_name", "")),
        bank_account=_esc(company.get("bank_account", "")),
        bank_iban=_esc(company.get("bank_iban", "")),
        bank_swift=_esc(company.get("bank_swift", "")),
        invoice_prefix=_esc(sqlite_db.get_setting("invoice_prefix", company.get("invoice_prefix", "INV-"))),
        invoice_next_number=_esc(sqlite_db.get_setting("invoice_next_number", company.get("invoice_next_number", 1))),
        invoice_notes=_esc(company.get("invoice_notes", "")),
        invoice_footer=_esc(company.get("invoice_footer", "")),
        tax_regime=_esc(company.get("tax_regime", "")),
        qr_t=_s(company.get("qrcode_enabled"), True),
        qr_f=_s(company.get("qrcode_enabled"), False),
        pe_t=_s(db.get("polling_enabled"), True),
        pe_f=_s(db.get("polling_enabled"), False),
        poll_ms=db.get("polling_interval_ms", 30000),
        max_concurrent=queue.get("max_concurrent", 5),
        max_retries=queue.get("max_retries", 4),
        retry_base_delay_ms=queue.get("retry_base_delay_ms", 2000),
        max_queue_size=queue.get("max_queue_size", 10000),
        job_timeout_ms=queue.get("job_timeout_ms", 60000),
        st_t=_s(validation.get("strict_mode"), True),
        st_f=_s(validation.get("strict_mode"), False),
        tolerance=validation.get("tolerance_amount", 0.01),
        daily_max=rate_limit.get("daily_max", 2500),
        per_minute_max=rate_limit.get("per_minute_max", 100),
        alert_threshold=rate_limit.get("alert_threshold_percent", 80),
        nt_t=_s(notifications.get("enabled"), True),
        nt_f=_s(notifications.get("enabled"), False),
        ev_synced=_ck("invoice:synced" in notif_events),
        ev_val=_ck("invoice:validation_error" in notif_events),
        ev_dlq=_ck("queue:dlq_added" in notif_events),
        ev_dbf=_ck("db:connection_failed" in notif_events),
        ev_dbe=_ck("db:polling_error" in notif_events),
        ll_d=_s(log_level, "debug"),
        ll_i=_s(log_level, "info"),
        ll_w=_s(log_level, "warning"),
        ll_e=_s(log_level, "error"),
        dash_port=_esc(dash.get("port", 3000)),
        auth_t=_s(dash.get("auth_enabled"), True),
        login_email=_esc(dash.get("login_email", "")),
        session_max_hours=_esc(dash.get("session_max_age_hours", 24)),
        host=_esc(dash.get("host", "0.0.0.0")),
    )

    return _page(body)


def _bool(val):
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "on")
    return bool(val)


def _apply_bounded(data, target, specs, errors):
    """Copie des entiers bornes de data vers target (CF-13/22/23).

    specs : tuples (cle, min, max). Toute valeur hors plage ou non entiere
    alimente `errors` au lieu d'etre ecrite silencieusement.
    """
    for key, lo, hi in specs:
        if key not in data:
            continue
        try:
            val = int(data[key])
        except (ValueError, TypeError):
            errors.append("{}: valeur entiere attendue".format(key))
            continue
        if val < lo or val > hi:
            errors.append("{}: doit etre entre {} et {}".format(key, lo, hi))
            continue
        target[key] = val


def _validate_url(val, label):
    """Valide le format d'une URL http(s) (CF-14) et la retourne nettoyee."""
    from urllib.parse import urlparse
    url = str(val or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("{}: URL http(s) invalide".format(label))
    if parsed.username or parsed.password:
        raise ValueError("{}: les identifiants dans l'URL sont interdits".format(label))
    return url


def _is_private_host(url):
    """True si l'hote de l'URL est prive/loopback (sans resolution DNS — les
    noms internes courants et les IP litterales seulement). Sert a AVERTIR,
    pas a bloquer : un TCC Server auto-heberge peut etre local."""
    import ipaddress
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost",) or host.endswith((".local", ".internal", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


@bp.route("/api/db/test", methods=["GET"])
@_login_required
def api_db_test():
    return jsonify(ping_database())


@bp.route("/api/sfec/test", methods=["GET"])
@_login_required
def api_sfec_test():
    return jsonify(check_health())


@bp.route("/api/sfec/certify", methods=["POST"])
@_login_required
def api_sfec_certify():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"success": False, "error": "invoice_id requis"}), 400

    if str(invoice_id).startswith("INV-"):
        try:
            inv_num = int(str(invoice_id).replace("INV-", ""))
        except ValueError:
            return jsonify({"success": False, "error": "Format ID invalide: {}".format(invoice_id)}), 400

        inv = invoice_engine.get_invoice(inv_num)
        if not inv:
            return jsonify({"success": False, "error": "Facture SQLite #{} non trouvee".format(inv_num)}), 404
        statut = (inv.get("statut") or "").lower()
        if statut in ("", "brouillon", "draft"):
            return jsonify({"success": False,
                            "error": "Validez la facture avant de la certifier SFEC"}), 422
        if invoice_engine.is_invoice_locked(inv):
            return jsonify({"success": False,
                            "error": "Facture deja certifiee SFEC ou synchronisee Sage"}), 409

        try:
            result = certify_sqlite_invoice(inv)
            user_auth.audit("facture.certifiee", "Certification SFEC {} #{}".format(
                result.get("certification_number", ""), inv.get("numero", "")),
                email=(_current_identity() or {}).get("email", ""))
            return jsonify(result)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    return jsonify(certify_single(invoice_id))


@bp.route("/api/sfec/to-monitor", methods=["POST"])
@_login_required
def api_sfec_to_monitor():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"success": False, "error": "invoice_id requis"}), 400
    try:
        from app.sync.engine import mark_invoice_to_monitor
        mark_invoice_to_monitor(invoice_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/sfec/debug", methods=["POST"])
@_login_required
def api_sfec_debug():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"error": "invoice_id requis"}), 400
    cache = get_cache()
    invoice = None
    for inv in cache.get("sales_invoices", []):
        if inv["id"] == invoice_id:
            invoice = inv
            break
    if not invoice:
        return jsonify({"error": "Facture non trouvee: {}".format(invoice_id)}), 404
    payload = preview_sfec_payload(invoice)
    return jsonify({"ok": True, "payload": payload, "invoice_data": {k: v for k, v in invoice.items() if k != "lignes"}})


@bp.route("/api/sfec/validate", methods=["POST"])
@_login_required
def api_sfec_validate():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"error": "invoice_id requis"}), 400
    cache = get_cache()
    invoice = None
    for inv in cache.get("sales_invoices", []):
        if inv["id"] == invoice_id:
            invoice = inv
            break
    if not invoice:
        return jsonify({"error": "Facture non trouvee: {}".format(invoice_id)}), 404
    payload = preview_sfec_payload(invoice)
    errors = validate_sfec_payload(payload)
    return jsonify({"ok": len(errors) == 0, "errors": errors, "payload": payload})


@bp.route("/api/sfec/sync", methods=["POST"])
@_login_required
def api_sfec_sync():
    def _do_sync():
        sync_sfec_invoices()
    t = threading.Thread(target=_do_sync, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Sync lancee en arriere-plan"})


@bp.route("/api/sfec/certified-from-api", methods=["GET"])
@_login_required
def api_sfec_certified_from_api():
    try:
        client = SfecClient()
        result = client.list_invoices(page=1, page_size=100)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/tax-rates", methods=["GET"])
@_login_required
def api_tax_rates():
    return jsonify(fetch_tax_rates())


@bp.route("/api/tables", methods=["GET"])
@_login_required
def api_tables():
    return jsonify(list_all_tables())


@bp.route("/api/config", methods=["GET"])
@_login_required
def api_get_config():
    return jsonify(_redact_config(get_config()))


@bp.route("/api/config/db", methods=["POST"])
@_login_required
def api_config_db():
    cfg = get_config()
    db = dict(cfg.get("db", {}))
    data = request.get_json(silent=True) or {}
    for key in ("server", "database", "user", "password"):
        if key in data and not (key == "password" and data[key] == ""):
            db[key] = data[key]
    # CF-13/CF-23 : plages numeriques validees (refus 400 au lieu d'ignorer).
    errors = []
    _apply_bounded(data, db, (
        ("port", 1, 65535),
        ("polling_interval_ms", 1000, 3600000),
        ("pool_max", 1, 100),
        ("pool_min", 0, 100),
        ("pool_idle_timeout_ms", 1000, 86400000),
        ("connection_timeout_ms", 1000, 600000),
        ("request_timeout_ms", 1000, 3600000),
    ), errors)
    if db.get("pool_min", 0) > db.get("pool_max", 1):
        errors.append("pool_min ne peut pas depasser pool_max")
    for key in ("trusted_connection", "encrypt", "trust_server_certificate", "polling_enabled"):
        if key in data:
            db[key] = _bool(data[key])
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    cfg["db"] = db
    save_config(cfg)
    return jsonify({"ok": True})


@bp.route("/api/config/sfec", methods=["POST"])
@_login_required
def api_config_sfec():
    cfg = get_config()
    sfec = dict(cfg.get("sfec", {}))
    data = request.get_json(silent=True) or {}
    errors = []
    warnings = []
    for key in ("api_key", "certify_status"):
        if key in data and not (key == "api_key" and data[key] == ""):
            sfec[key] = data[key]
    # CF-14 : URLs validees (format) + avertissement si hote prive (SSRF) —
    # non bloquant car le middleware TCC Server peut etre auto-heberge.
    for key in ("base_url", "sandbox_url"):
        if key in data:
            try:
                sfec[key] = _validate_url(data[key], key)
            except ValueError as exc:
                errors.append(str(exc))
            else:
                if _is_private_host(sfec[key]):
                    warnings.append("{} : hote prive/local — verifiez qu'il s'agit bien du serveur SFEC/TCC".format(key))
    # CF-15 : statut de certification restreint aux valeurs SFEC connues.
    if "certify_status" in data:
        allowed = ("SAISI", "CONFIRME", "A COMPTABILISER", "COMPTABILISE", "LETTRE")
        if str(data["certify_status"]).strip().upper() not in allowed:
            errors.append("certify_status: valeur attendue parmi {}".format(", ".join(allowed)))
    for key in ("use_sandbox", "enabled"):
        if key in data:
            sfec[key] = _bool(data[key])
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    cfg["sfec"] = sfec
    save_config(cfg)
    resp = {"ok": True}
    if warnings:
        resp["warnings"] = warnings
    return jsonify(resp)


@bp.route("/api/config/company", methods=["POST"])
@_login_required
def api_config_company():
    cfg = get_config()
    company = dict(cfg.get("company", {}))
    data = request.get_json(silent=True) or {}
    str_fields = ("name", "tax_number", "address", "city", "country", "phone", "email",
                  "website", "rc_number", "bank_name", "bank_account", "bank_iban", "bank_swift",
                  "currency", "slogan", "default_payment_terms",
                  "invoice_prefix", "invoice_notes", "invoice_footer", "tax_regime")
    for key in str_fields:
        if key in data:
            company[key] = data[key]
    if "invoice_next_number" in data:
        try:
            company["invoice_next_number"] = int(data["invoice_next_number"])
        except (ValueError, TypeError):
            pass
    # CF-09 : la numerotation reelle lit la table settings (generate_invoice_number)
    # — on y propage le prefixe et le prochain numero saisis dans /config,
    # sinon ces champs n'avaient aucun effet.
    try:
        from app.storage.db import set_setting
        if "invoice_prefix" in data:
            prefix = str(data.get("invoice_prefix") or "").strip()
            if prefix:
                set_setting("invoice_prefix", prefix)
                set_setting("invoice_format", prefix + "{:06d}")
        if "invoice_next_number" in data:
            try:
                nxt = int(data["invoice_next_number"])
                if nxt >= 1:
                    set_setting("invoice_next_number", str(nxt))
            except (ValueError, TypeError):
                pass
    except Exception:
        pass
    for key in ("qrcode_enabled",):
        if key in data:
            company[key] = _bool(data[key])
    cfg["company"] = company
    save_config(cfg)
    return jsonify({"ok": True})


@bp.route("/api/config/sync", methods=["POST"])
@_login_required
def api_config_sync():
    cfg = get_config()
    data = request.get_json(silent=True) or {}

    db = dict(cfg.get("db", {}))
    if "polling_enabled" in data:
        db["polling_enabled"] = _bool(data["polling_enabled"])
    errors = []
    _apply_bounded(data, db, (("polling_interval_ms", 1000, 3600000),), errors)
    cfg["db"] = db

    queue = dict(cfg.get("queue", {}))
    _apply_bounded(data, queue, (
        ("max_concurrent", 1, 50),
        ("max_retries", 0, 20),
        ("retry_base_delay_ms", 100, 3600000),
        ("max_queue_size", 1, 100000),
        ("job_timeout_ms", 1000, 3600000),
    ), errors)
    cfg["queue"] = queue

    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    save_config(cfg)
    return jsonify({"ok": True})


@bp.route("/api/config/validation", methods=["POST"])
@_login_required
def api_config_validation():
    cfg = get_config()
    data = request.get_json(silent=True) or {}

    validation = dict(cfg.get("validation", {}))
    if "strict_mode" in data:
        validation["strict_mode"] = _bool(data["strict_mode"])
    errors = []
    if "tolerance_amount" in data:
        try:
            tol = float(data["tolerance_amount"])
            if tol < 0 or tol > 1000000000:
                errors.append("tolerance_amount: doit etre entre 0 et 1000000000")
            else:
                validation["tolerance_amount"] = tol
        except (ValueError, TypeError):
            errors.append("tolerance_amount: nombre attendu")
    cfg["validation"] = validation

    rate_limit = dict(cfg.get("rate_limit", {}))
    _apply_bounded(data, rate_limit, (
        ("daily_max", 1, 1000000),
        ("per_minute_max", 1, 100000),
        ("alert_threshold_percent", 0, 100),
    ), errors)
    # CF-22 : controle croise — la limite minute ne depasse pas la limite jour.
    if rate_limit.get("per_minute_max", 1) > rate_limit.get("daily_max", 1):
        errors.append("per_minute_max ne peut pas depasser daily_max")
    cfg["rate_limit"] = rate_limit

    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    save_config(cfg)
    return jsonify({"ok": True})


@bp.route("/api/config/notifications", methods=["POST"])
@_login_required
def api_config_notifications():
    cfg = get_config()
    data = request.get_json(silent=True) or {}
    notifications = dict(cfg.get("notifications", {}))
    if "enabled" in data:
        notifications["enabled"] = _bool(data["enabled"])

    events = []
    event_keys = {
        "evt_synced": "invoice:synced",
        "evt_val_error": "invoice:validation_error",
        "evt_dlq": "queue:dlq_added",
        "evt_db_fail": "db:connection_failed",
        "evt_db_err": "db:polling_error",
    }
    for form_key, event_name in event_keys.items():
        if form_key in data and _bool(data[form_key]):
            events.append(event_name)
    notifications["events"] = events

    cfg["notifications"] = notifications
    save_config(cfg)
    return jsonify({"ok": True})


@bp.route("/api/config/system", methods=["POST"])
@_login_required
def api_config_system():
    cfg = get_config()
    data = request.get_json(silent=True) or {}

    if "log_level" in data:
        cfg["log_level"] = data["log_level"]

    dash = dict(cfg.get("dashboard", {}))
    for key in ("login_email", "host"):
        if key in data:
            dash[key] = data[key]
    # Le mot de passe de bootstrap n'est jamais réaffiché ; un champ vide
    # conserve la valeur existante. Le secret de session n'est plus éditable
    # depuis l'interface : il est généré/validé par _get_secret_key().
    if data.get("login_password"):
        password = str(data["login_password"])
        if len(password) < 10 or not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
            return jsonify({"ok": False, "error": "Mot de passe trop faible (10 caracteres minimum, lettres et chiffres)"}), 400
        dash["login_password"] = password
    if "port" in data:
        try:
            dash["port"] = int(data["port"])
        except (ValueError, TypeError):
            pass
    if "auth_enabled" in data:
        if not _bool(data["auth_enabled"]):
            return jsonify({
                "ok": False,
                "error": "La desactivation de l'authentification via l'interface est interdite",
            }), 400
        dash["auth_enabled"] = True
    if "session_max_age_hours" in data:
        try:
            hours = int(data["session_max_age_hours"])
            if 1 <= hours <= 24 * 30:
                dash["session_max_age_hours"] = hours
        except (ValueError, TypeError):
            pass
    cfg["dashboard"] = dash

    save_config(cfg)
    user_auth.audit("config.systeme", "Configuration systeme mise a jour",
                    email=(_current_identity() or {}).get("email", ""))
    return jsonify({"ok": True})


@bp.route("/api/config/dashboard", methods=["POST"])
@_login_required
def api_config_dashboard():
    return api_config_system()


@bp.route("/api/config/reload", methods=["POST"])
@_login_required
def api_config_reload():
    try:
        result = apply_config()
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/config/export", methods=["GET"])
@_login_required
def api_config_export():
    from flask import send_file, after_this_request
    import tempfile
    cfg = _redact_config(get_config())
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(cfg, tmp, indent=2, ensure_ascii=False)
    tmp.close()
    tmp_path = tmp.name

    @after_this_request
    def cleanup(response):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return response

    return send_file(tmp_path, as_attachment=True, download_name="config.json", mimetype="application/json")


@bp.route("/api/config/import", methods=["POST"])
@_login_required
def api_config_import():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Donnees JSON invalides (objet requis)"}), 400

    allowed_sections = {"db", "sfec", "company", "dashboard", "queue",
                        "validation", "notifications", "rate_limit", "log_level"}
    unknown = set(data.keys()) - allowed_sections
    if unknown:
        return jsonify({"ok": False, "error": "Sections inconnues: {}".format(", ".join(sorted(unknown)))}), 400

    required_sections = ("db", "sfec", "company")
    for section in required_sections:
        if section not in data:
            return jsonify({"ok": False, "error": "Section '{}' manquante".format(section)}), 400
        if not isinstance(data[section], dict):
            return jsonify({"ok": False, "error": "La section '{}' doit etre un objet".format(section)}), 400

    dashboard = data.get("dashboard")
    if isinstance(dashboard, dict):
        if dashboard.get("auth_enabled") is False or str(dashboard.get("auth_enabled")).lower() == "false":
            return jsonify({
                "ok": False,
                "error": "L'import ne peut pas desactiver l'authentification",
            }), 400
        dashboard.pop("session_secret", None)

    try:
        merged = _merge_import_config(get_config(), data)
        save_config(merged)
        user_auth.audit("config.import", "Configuration importee",
                        email=(_current_identity() or {}).get("email", ""))
        return jsonify({"ok": True, "message": "Configuration importee avec succes"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/tax-rates/local", methods=["GET"])
@_login_required
def api_local_tax_rates():
    return jsonify(pos_engine.list_tax_rates())



