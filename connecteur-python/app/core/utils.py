"""Helpers génériques partagés entre les couches."""
import re


DEFAULT_COUNTER_CUSTOMER_CODES = frozenset({"CLI-CPT"})
DEFAULT_COUNTER_NAME_KEYWORDS = ("COMPTOIR", "COMPTANT")


def _normalise_code_list(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        value = [value]
    return frozenset(
        str(item).strip().upper() for item in value if str(item).strip()
    )


def _normalise_keyword_list(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        value = [value]
    return tuple(
        str(item).strip().upper() for item in value if str(item).strip()
    )


def is_counter_customer(code="", name="", sfec_cfg=None):
    """Detecte un client comptoir sans modifier les donnees client.

    Le code client est prioritaire. Le nom sert de repli pour les comptes
    comptoir qui n'utilisent pas exactement le code `CLI-CPT`.
    """
    cfg = sfec_cfg or {}
    codes = _normalise_code_list(
        cfg.get("counter_customer_codes"),
        DEFAULT_COUNTER_CUSTOMER_CODES,
    )
    keywords = _normalise_keyword_list(
        cfg.get("counter_name_keywords"),
        DEFAULT_COUNTER_NAME_KEYWORDS,
    )

    normalized_code = str(code or "").strip().upper()
    if normalized_code and normalized_code in codes:
        return True

    normalized_name = str(name or "").strip().upper()
    return bool(normalized_name) and any(
        keyword in normalized_name for keyword in keywords
    )


def safe_float(val, default=0.0):
    """Convertit une valeur en float, en gérant la virgule décimale française."""
    if val is None:
        return default
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return default


def safe_int(val, default=0):
    try:
        return int(safe_float(val, default))
    except (ValueError, TypeError):
        return default


def safe_str(val, default=""):
    if val is None:
        return default
    s = str(val).strip()
    return s if s else default


def normalize_date(val):
    """Normalise plusieurs formats de date vers YYYY-MM-DD, ou None."""
    if not val:
        return None
    s = str(val).strip()
    if not s or s == "1900-01-01":
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "{}-{}-{}".format(m.group(1), m.group(2), m.group(3))
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return "{}-{}-{}".format(m.group(3), m.group(2), m.group(1))
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", s)
    if m:
        return "{}-{}-{}".format(m.group(3), m.group(2), m.group(1))
    nums = re.findall(r"\d+", s)
    if len(nums) >= 3:
        for i, n in enumerate(nums):
            if len(n) == 4:
                year = n
                rest = nums[:i] + nums[i + 1:]
                if len(rest) >= 2:
                    return "{}-{}-{}".format(year, rest[1].zfill(2), rest[0].zfill(2))
    return None

def fmt_money(val, decimals=0):
    """Formate un montant depuis n'importe quel type (float, str, None).

    - décale la virgule française et arrondit à `decimals` décimales ;
    - ajoute les séparateurs de milliers ;
    - 0.0 → "0", 1500 → "1,500".
    """
    try:
        x = float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        x = 0.0
    return "{:,.{}f}".format(x, decimals)