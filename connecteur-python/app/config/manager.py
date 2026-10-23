import json
import os
import sys
import copy
import tempfile
import threading

if getattr(sys, 'frozen', False):
    _BUNDLE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    CONFIG_DIR = os.path.dirname(sys.executable)
else:
    _BUNDLE_DIR = None
    CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_config_candidates = [os.path.join(CONFIG_DIR, "config.json")]
if _BUNDLE_DIR and _BUNDLE_DIR != CONFIG_DIR:
    _config_candidates.append(os.path.join(_BUNDLE_DIR, "config.json"))

CONFIG_FILE = None
for _c in _config_candidates:
    if os.path.exists(_c):
        CONFIG_FILE = _c
        break
if CONFIG_FILE is None:
    CONFIG_FILE = _config_candidates[0]
# Surcharge réservée aux tests isolés (copie temporaire de config.json).
CONFIG_FILE = os.environ.get("TCONNECTOR_CONFIG_FILE") or CONFIG_FILE

_lock = threading.RLock()
_config = None

# Valeurs d'exemple qui ne doivent jamais signer une session en production.
INSECURE_SESSION_SECRETS = {
    "", "CHANGE-ME", "CHANGE-ME-IN-PRODUCTION", "change-me", "secret",
}


def _restrict_permissions(path):
    """Les secrets de config.json ne doivent pas etre lisibles par tous."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _defaults():
    return {
        "db": {
            "server": "",
            "port": 1433,
            "database": "",
            "user": "",
            "password": "",
            "trusted_connection": False,
            "encrypt": False,
            "trust_server_certificate": True,
            "pool_max": 10,
            "pool_min": 2,
            "pool_idle_timeout_ms": 30000,
            "connection_timeout_ms": 15000,
            "request_timeout_ms": 30000,
            "polling_interval_ms": 30000,
            "polling_enabled": True,
        },
        "sfec": {
            "enabled": True,
            "api_key": "",
            "base_url": "https://api.sfec.gouv.cg",
            "sandbox_url": "https://sandbox.api.sfec.gouv.cg",
            "use_sandbox": True,
            "certify_status": "A COMPTABILISER",
        },
        "company": {
            "name": "",
            "slogan": "",
            "address": "",
            "city": "",
            "country": "",
            "phone": "",
            "email": "",
            "website": "",
            "tax_number": "",
            "rc_number": "",
            "logo_base64": "",
            "logo_file_name": "",
            "currency": "XAF",
            "currency_symbol": "FCFA",
            "default_payment_terms": "Net 30",
            "bank_name": "",
            "bank_account": "",
            "bank_iban": "",
            "bank_swift": "",
            "invoice_prefix": "INV-",
            "invoice_next_number": 1,
            "invoice_notes": "",
            "invoice_footer": "",
            "tax_regime": "",
            "qrcode_enabled": True,
        },
        "queue": {
            "max_concurrent": 5,
            "max_retries": 4,
            "retry_base_delay_ms": 2000,
            "max_queue_size": 10000,
            "job_timeout_ms": 60000,
        },
        "validation": {
            "strict_mode": True,
            "tolerance_amount": 0.01,
        },
        "notifications": {
            "enabled": True,
            "events": [
                "invoice:synced",
                "invoice:validation_error",
                "queue:dlq_added",
                "db:connection_failed",
                "db:polling_error",
            ],
        },
        "rate_limit": {
            "daily_max": 2500,
            "per_minute_max": 100,
            "alert_threshold_percent": 80,
        },
        "dashboard": {
            "port": 3000,
            # Ecoute locale par defaut : un acces distant doit passer par un
            # tunnel SSH ou un reverse-proxy authentifie (proposition 09).
            "host": "127.0.0.1",
            "auth_enabled": True,
            "session_secret": "CHANGE-ME-IN-PRODUCTION",
            "login_email": "admin@example.com",
            "login_password": "CHANGE-ME",
            "session_max_age_hours": 24,
        },
        "pos": {
            "stock_control": True,
        },
        "print_hijack": {
            "enabled": False,
            "watch_dir": "data/WATCH/IN",
            "out_dir": "data/WATCH/OUT",
            "done_dir": "data/WATCH/DONE",
            "quarantine_dir": "data/WATCH/QUARANTINE",
            "printer_name": "",
            "sumatra_path": "",
            "sumatra_min_version": "3.5.2",
            "auto_install_sumatra": True,
            "stable_delay_s": 2,
            "stable_retries": 5,
            "poll_interval_s": 5,
            "dedup_window_min": 10,
            "max_pages": 50,
            "max_size_mb": 20,
            "wait_for_certification": True,
            "certif_timeout_min": 15,
            "on_uncertified": "watermark",
            "dry_run": False,
        },
        "log_level": "info",
    }


def _deep_merge(base, override):
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config():
    global _config
    with _lock:
        defaults = _defaults()
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    file_cfg = json.load(f)
                _config = _deep_merge(defaults, file_cfg)
            except Exception:
                _config = defaults
        else:
            _config = defaults
            save_config(_config)
        # S1 (sécurité, audit 2026-09) : la clé API SFEC peut être fournie par
        # la variable d'environnement SFEC_API_KEY, qui PREND LA PRIORITÉ sur
        # la valeur de config.json. config.json (gitignoré) reste le fallback
        # pour les installations existantes — la clé n'y est PAS supprimée.
        # L'overlay est appliqué ici, sur la config chargée, afin que TOUS les
        # lecteurs (get_sfec_config, engine.py, pos.py, routes/config.py, ...)
        # voient la clé effective sans modification de leur code.
        env_key = os.environ.get("SFEC_API_KEY", "").strip()
        if env_key:
            _config.setdefault("sfec", {})["api_key"] = env_key
        _apply_env_security_overrides(_config)
        if os.path.exists(CONFIG_FILE):
            _restrict_permissions(CONFIG_FILE)
    return _config


def _apply_env_security_overrides(cfg):
    """Surcharges d'environnement : jamais persistees dans config.json.

    Permet de sortir les secrets du fichier (ou de la sauvegarde) sans
    casser les installations existantes qui utilisent encore config.json.
    """
    dash = cfg.setdefault("dashboard", {})
    for env_name, key in (
        ("TCONNECTOR_SESSION_SECRET", "session_secret"),
        ("TCONNECTOR_LOGIN_EMAIL", "login_email"),
        ("TCONNECTOR_LOGIN_PASSWORD", "login_password"),
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            dash[key] = value

    db_password = os.environ.get("TCONNECTOR_DB_PASSWORD", "").strip()
    if db_password:
        cfg.setdefault("db", {})["password"] = db_password
    return cfg


def validate_security_settings(cfg):
    """Refuse une configuration d'authentification evidente en production.

    Les valeurs d'exemple sont bloquees ; un secret court reste accepte mais
    un avertissement est retourne (la securite web genere deja un secret
    ephemere dans ce cas).
    """
    dash = (cfg or {}).get("dashboard", {}) or {}
    warnings = []
    if not dash.get("auth_enabled", True):
        raise RuntimeError(
            "dashboard.auth_enabled=false interdit : activer l'authentification"
        )
    secret = str(dash.get("session_secret") or "")
    if secret in INSECURE_SESSION_SECRETS:
        raise RuntimeError(
            "dashboard.session_secret non configure : definir "
            "TCONNECTOR_SESSION_SECRET ou une valeur d'au moins 32 caracteres"
        )
    if len(secret) < 32:
        warnings.append(
            "dashboard.session_secret court (<32 caracteres) : definir "
            "TCONNECTOR_SESSION_SECRET"
        )
    host = str(dash.get("host") or "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1"):
        warnings.append(
            "dashboard.host={} : acces distant expose, utiliser un tunnel "
            "SSH ou un reverse-proxy authentifie".format(host)
        )
    return warnings


def _restore_persisted_security_values(clean):
    """Ne jamais ecrire dans config.json une valeur venue de l'environnement."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            persisted = json.load(f)
    except Exception:
        persisted = {}
    for env_name, section, key in (
        ("TCONNECTOR_SESSION_SECRET", "dashboard", "session_secret"),
        ("TCONNECTOR_LOGIN_EMAIL", "dashboard", "login_email"),
        ("TCONNECTOR_LOGIN_PASSWORD", "dashboard", "login_password"),
        ("TCONNECTOR_DB_PASSWORD", "db", "password"),
    ):
        env_value = os.environ.get(env_name, "").strip()
        if not env_value:
            continue
        target = clean.setdefault(section, {})
        if target.get(key) != env_value:
            continue
        previous = (persisted.get(section) or {}).get(key)
        target[key] = previous if previous is not None else ""


def save_config(cfg):
    global _config
    with _lock:
        clean = copy.deepcopy(cfg)
        # La clé fournie par l'environnement ne doit JAMAIS être persistée
        # dans config.json (audit : fuite de secret via export/sauvegarde).
        env_key = os.environ.get("SFEC_API_KEY", "").strip()
        if env_key and clean.get("sfec", {}).get("api_key") == env_key:
            clean.setdefault("sfec", {})["api_key"] = ""
        _restore_persisted_security_values(clean)

        directory = os.path.dirname(CONFIG_FILE) or "."
        os.makedirs(directory, exist_ok=True)
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(clean, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, CONFIG_FILE)
            tmp_path = None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        _config = copy.deepcopy(clean)
        # Les surcharges d'environnement restent effectives en memoire apres
        # une sauvegarde, sans avoir ete ecrites dans le fichier.
        env_key = os.environ.get("SFEC_API_KEY", "").strip()
        if env_key:
            _config.setdefault("sfec", {})["api_key"] = env_key
        _apply_env_security_overrides(_config)


def get_config():
    global _config
    if _config is None:
        return load_config()
    return _config


def get_db_config():
    return get_config().get("db", {})


def get_sfec_config():
    return get_config().get("sfec", {})


def get_company_config():
    return get_config().get("company", {})


def get_dashboard_config():
    return get_config().get("dashboard", {})


def update_section(section, data):
    cfg = get_config()
    cfg[section] = data
    save_config(cfg)
    return cfg
