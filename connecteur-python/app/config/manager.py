import json
import os
import sys
import copy
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

_lock = threading.RLock()
_config = None


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
            "host": "0.0.0.0",
            "auth_enabled": True,
            "session_secret": "CHANGE-ME-IN-PRODUCTION",
            "login_email": "admin@example.com",
            "login_password": "CHANGE-ME",
            "session_max_age_hours": 24,
        },
        "pos": {
            "stock_control": True,
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
    return _config


def save_config(cfg):
    global _config
    with _lock:
        _config = copy.deepcopy(cfg)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(_config, f, indent=2, ensure_ascii=False)


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
