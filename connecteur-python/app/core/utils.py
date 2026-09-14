"""Helpers génériques partagés entre les couches."""
import re


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