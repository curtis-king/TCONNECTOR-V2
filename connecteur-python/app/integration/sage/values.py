"""Petites conversions de valeurs Sage (devises, nombres)."""
from app.config.manager import get_db_config


def _safe_float(val, default=0.0):
    """Convert value to float, handling French comma decimals."""
    if val is None:
        return default
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return default

_DEFAULT_DEVISE_MAP = {"0": "XAF", "3": "XAF", "5": "XAF"}

def get_devise_map():
    cfg = get_db_config()
    custom = cfg.get("devise_map") or {}
    m = dict(_DEFAULT_DEVISE_MAP)
    m.update({str(k): v for k, v in custom.items()})
    return m

def _map_devise(val):
    m = get_devise_map()
    return m.get(str(val or "").strip(), m.get("0", "XAF"))
