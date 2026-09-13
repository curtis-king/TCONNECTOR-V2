"""Parking config — Configuration : /config /api/config* /api/db/* /api/tables /api/sfec/* /api/tax-rates*.

Handlers déplacés mécaniquement depuis app/web/dashboard.py (étape A3).
AUCUN décorateur ici : les routes sont enregistrées dans dashboard.py
via app.add_url_rule(). Corps des fonctions strictement inchangés.
"""

import json
import os
import tempfile
import threading
from app.config.manager import get_config, save_config
from app.domain import invoices as invoice_engine, pos as pos_engine
from app.integration.sage.database import fetch_tax_rates, list_all_tables, mark_to_monitor, ping_database
from app.integration.sfec.client import SfecClient
from app.integration.sfec.endpoints import certify_sqlite_invoice, check_health, preview_sfec_payload, validate_sfec_payload
from app.sync.engine import apply_config, certify_single, get_cache, sync_sfec_invoices
from app.web.auth import user_auth
from flask import after_this_request, jsonify, render_template, request, send_file
from app.web.parking.common import _esc, _page


def __getattr__(name):
    """Filet de sécurité : délégation vers app.web.dashboard pour tout nom
    non résolu (uniquement effectif sur accès attribut du module). Les noms
    réellement utilisés par les handlers sont liés explicitement en bas de
    ce module (voir section « Liaison dashboard »)."""
    from app.web import dashboard as _dashboard
    return getattr(_dashboard, name)


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
        server=_esc(db.get("server", "")),
        port=_esc(db.get("port", 1433)),
        database=_esc(db.get("database", "")),
        user=_esc(db.get("user", "")),
        password=_esc(db.get("password", "")),
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
        api_key=_esc(sfec.get("api_key", "")),
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
        invoice_prefix=_esc(company.get("invoice_prefix", "INV-")),
        invoice_next_number=_esc(company.get("invoice_next_number", 1)),
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
        auth_f=_s(dash.get("auth_enabled"), False),
        login_email=_esc(dash.get("login_email", "")),
        login_password=_esc(dash.get("login_password", "")),
        session_secret=_esc(dash.get("session_secret", "")),
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


def api_db_test():
    return jsonify(ping_database())


def api_sfec_test():
    return jsonify(check_health())


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

        try:
            result = certify_sqlite_invoice(inv)
            user_auth.audit("facture.certifiee", "Certification SFEC {} #{}".format(
                result.get("sfec_statut", ""), result.get("numero", "")),
                email=(_current_identity() or {}).get("email", ""))
            return jsonify(result)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    return jsonify(certify_single(invoice_id))


def api_sfec_to_monitor():
    data = request.get_json(silent=True) or {}
    invoice_id = data.get("invoice_id")
    if not invoice_id:
        return jsonify({"success": False, "error": "invoice_id requis"}), 400
    try:
        mark_to_monitor(invoice_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


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


def api_sfec_sync():
    def _do_sync():
        sync_sfec_invoices()
    t = threading.Thread(target=_do_sync, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Sync lancee en arriere-plan"})


def api_sfec_certified_from_api():
    try:
        client = SfecClient()
        result = client.list_invoices(page=1, page_size=100)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def api_tax_rates():
    return jsonify(fetch_tax_rates())


def api_tables():
    return jsonify(list_all_tables())


def api_get_config():
    cfg = get_config()
    masked = dict(cfg)
    for section in ("db", "sfec"):
        if section in masked:
            masked[section] = dict(masked[section])
            for key in ("password", "api_key"):
                if key in masked[section]:
                    masked[section][key] = "***"
    return jsonify(masked)


def api_config_db():
    cfg = get_config()
    db = dict(cfg.get("db", {}))
    data = request.get_json(silent=True) or {}
    for key in ("server", "database", "user", "password"):
        if key in data:
            db[key] = data[key]
    for key in ("port", "polling_interval_ms", "pool_max", "pool_min",
                "pool_idle_timeout_ms", "connection_timeout_ms", "request_timeout_ms"):
        if key in data:
            try:
                db[key] = int(data[key])
            except (ValueError, TypeError):
                pass
    for key in ("trusted_connection", "encrypt", "trust_server_certificate", "polling_enabled"):
        if key in data:
            db[key] = _bool(data[key])
    cfg["db"] = db
    save_config(cfg)
    return jsonify({"ok": True})


def api_config_sfec():
    cfg = get_config()
    sfec = dict(cfg.get("sfec", {}))
    data = request.get_json(silent=True) or {}
    for key in ("api_key", "base_url", "sandbox_url", "certify_status"):
        if key in data:
            sfec[key] = data[key]
    for key in ("use_sandbox", "enabled"):
        if key in data:
            sfec[key] = _bool(data[key])
    cfg["sfec"] = sfec
    save_config(cfg)
    return jsonify({"ok": True})


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
    for key in ("qrcode_enabled",):
        if key in data:
            company[key] = _bool(data[key])
    cfg["company"] = company
    save_config(cfg)
    return jsonify({"ok": True})


def api_config_sync():
    cfg = get_config()
    data = request.get_json(silent=True) or {}

    db = dict(cfg.get("db", {}))
    if "polling_enabled" in data:
        db["polling_enabled"] = _bool(data["polling_enabled"])
    if "polling_interval_ms" in data:
        try:
            db["polling_interval_ms"] = int(data["polling_interval_ms"])
        except (ValueError, TypeError):
            pass
    cfg["db"] = db

    queue = dict(cfg.get("queue", {}))
    for key in ("max_concurrent", "max_retries", "retry_base_delay_ms", "max_queue_size", "job_timeout_ms"):
        if key in data:
            try:
                queue[key] = int(data[key])
            except (ValueError, TypeError):
                pass
    cfg["queue"] = queue

    save_config(cfg)
    return jsonify({"ok": True})


def api_config_validation():
    cfg = get_config()
    data = request.get_json(silent=True) or {}

    validation = dict(cfg.get("validation", {}))
    if "strict_mode" in data:
        validation["strict_mode"] = _bool(data["strict_mode"])
    if "tolerance_amount" in data:
        try:
            validation["tolerance_amount"] = float(data["tolerance_amount"])
        except (ValueError, TypeError):
            pass
    cfg["validation"] = validation

    rate_limit = dict(cfg.get("rate_limit", {}))
    for key in ("daily_max", "per_minute_max", "alert_threshold_percent"):
        if key in data:
            try:
                rate_limit[key] = int(data[key])
            except (ValueError, TypeError):
                pass
    cfg["rate_limit"] = rate_limit

    save_config(cfg)
    return jsonify({"ok": True})


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


def api_config_system():
    cfg = get_config()
    data = request.get_json(silent=True) or {}

    if "log_level" in data:
        cfg["log_level"] = data["log_level"]

    dash = dict(cfg.get("dashboard", {}))
    for key in ("login_email", "login_password", "session_secret", "host"):
        if key in data:
            dash[key] = data[key]
    if "port" in data:
        try:
            dash["port"] = int(data["port"])
        except (ValueError, TypeError):
            pass
    if "auth_enabled" in data:
        dash["auth_enabled"] = _bool(data["auth_enabled"])
    if "session_max_age_hours" in data:
        try:
            dash["session_max_age_hours"] = int(data["session_max_age_hours"])
        except (ValueError, TypeError):
            pass
    cfg["dashboard"] = dash

    save_config(cfg)
    return jsonify({"ok": True})


def api_config_dashboard():
    return api_config_system()


def api_config_reload():
    try:
        result = apply_config()
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def api_config_export():
    from flask import send_file, after_this_request
    import tempfile
    cfg = get_config()
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

    try:
        save_config(data)
        return jsonify({"ok": True, "message": "Configuration importee avec succes"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def api_local_tax_rates():
    return jsonify(pos_engine.list_tax_rates())


# ── Liaison dashboard ──
# Ces noms sont définis dans app/web/dashboard.py. On les lie ICI, en bas de
# module : quel que soit l'ordre d'import (dashboard d'abord ou parking
# d'abord), ils existent déjà dans le namespace de dashboard.py à ce stade —
# aucun import circulaire possible. Les corps des fonctions ci-dessus restent
# strictement inchangés (références globales résolues à l'exécution).
from app.web import dashboard as _dashboard
_current_identity = _dashboard._current_identity

del _dashboard
