"""Audit et durcissement des comptes/secrets locaux (proposition 09 §4).

Ne supprime jamais un compte : `--apply` desactive uniquement les comptes de
test connus, et refuse de retirer le dernier administrateur actif. Les
valeurs de secrets ne sont jamais affichees.
"""
import os
import sqlite3

from app.config.manager import INSECURE_SESSION_SECRETS
from app.storage import db as sqlite_db

KNOWN_TEST_ACCOUNTS = ("admin@admin.com", "admin@test.com")
DEFAULT_PASSWORDS = {"", "CHANGE-ME", "change-me", "admin", "admin123"}


def _connect(db_path=None):
    path = db_path or sqlite_db.DB_PATH
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _config_path(config_path=None):
    if config_path:
        return config_path
    from app.config.manager import CONFIG_FILE
    return CONFIG_FILE


def _sites_path():
    from app.print_hijack.sites import SITES_FILE
    return SITES_FILE


def _file_mode(path):
    try:
        return oct(os.stat(path).st_mode & 0o777)
    except OSError:
        return ""


def audit_security(config_path=None, db_path=None):
    """Rapport sans secret en clair."""
    import json

    path = _config_path(config_path)
    report = {
        "config_path": path,
        "config_mode": _file_mode(path),
        "sites_mode": _file_mode(_sites_path()),
        "config_permissions_ok": False,
        "dashboard": {},
        "test_accounts": [],
        "active_admins": 0,
        "warnings": [],
    }
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as e:  # noqa: BLE001
        cfg = {}
        report["warnings"].append("config.json illisible: {}".format(e))

    dash = (cfg.get("dashboard") or {})
    secret = str(dash.get("session_secret") or "")
    password = str(dash.get("login_password") or "")
    report["dashboard"] = {
        "host": dash.get("host", "127.0.0.1"),
        "auth_enabled": bool(dash.get("auth_enabled", True)),
        "session_secret_configured": secret not in INSECURE_SESSION_SECRETS,
        "session_secret_length": len(secret),
        "default_login_password": password in DEFAULT_PASSWORDS,
    }
    if report["config_mode"] not in ("", "0o600"):
        report["warnings"].append(
            "config.json lisible par le groupe/autres : chmod 600 recommande")
    if secret in INSECURE_SESSION_SECRETS:
        report["warnings"].append(
            "session_secret non configure (definir TCONNECTOR_SESSION_SECRET)")
    if password in DEFAULT_PASSWORDS:
        report["warnings"].append("mot de passe de bootstrap par defaut")
    if report["dashboard"]["host"] not in ("127.0.0.1", "localhost", "::1"):
        report["warnings"].append(
            "dashboard.host={} : acces distant expose".format(
                report["dashboard"]["host"]))
    report["config_permissions_ok"] = report["config_mode"] in ("", "0o600")

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, email, role, est_actif, derniere_connexion "
            "FROM utilisateurs WHERE lower(email) IN (?, ?) ORDER BY id",
            KNOWN_TEST_ACCOUNTS,
        ).fetchall()
        report["test_accounts"] = [dict(r) for r in rows]
        admin = conn.execute(
            "SELECT COUNT(*) AS n FROM utilisateurs "
            "WHERE role = 'admin' AND est_actif = 1"
        ).fetchone()
        report["active_admins"] = int(admin["n"] or 0)
    except Exception as e:  # noqa: BLE001
        report["warnings"].append("lecture utilisateurs impossible: {}".format(e))
    finally:
        conn.close()
    return report


def apply_security_audit(config_path=None, db_path=None):
    """Durcit les permissions et desactive prudemment les comptes de test."""
    report = audit_security(config_path, db_path)
    changed = {"files": [], "accounts": []}
    for path in (_config_path(config_path), _sites_path()):
        try:
            if os.path.exists(path):
                os.chmod(path, 0o600)
                changed["files"].append(path)
        except OSError as e:
            report["warnings"].append("chmod {}: {}".format(path, e))

    remaining_admins = report["active_admins"]
    try:
        conn = _connect(db_path)
    except Exception as e:  # noqa: BLE001
        report["warnings"].append("base utilisateurs inaccessible: {}".format(e))
        return {"report": report, "changed": changed}
    try:
        for account in report["test_accounts"]:
            if not account.get("est_actif"):
                continue
            if account.get("role") == "admin" and remaining_admins <= 1:
                report["warnings"].append(
                    "{} conserve : dernier administrateur actif".format(
                        account.get("email")))
                continue
            conn.execute(
                "UPDATE utilisateurs SET est_actif = 0 WHERE id = ?",
                (account["id"],),
            )
            if account.get("role") == "admin":
                remaining_admins -= 1
            changed["accounts"].append(account.get("email"))
        conn.commit()
    finally:
        conn.close()
    return {"report": report, "changed": changed}
