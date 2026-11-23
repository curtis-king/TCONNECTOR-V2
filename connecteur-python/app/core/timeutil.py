"""Horodatages UTC : source unique, formats persistés inchangés.

`datetime.utcnow()` est déprécié depuis Python 3.12. Les helpers ci-dessous
produisent exactement les mêmes valeurs que l'ancien code (UTC naïf, suffixe
« Z ») afin de ne pas invalider les données déjà stockées dans SQLite
(`last_sync_at`, `queued_at`, `started_at`, etc.).
"""
from datetime import datetime, timezone


def utcnow():
    """Datetime UTC timezone-aware (comparaisons, calculs de durée)."""
    return datetime.now(timezone.utc)


def utcnow_naive():
    """Datetime UTC naïf (paramètres SQL, anciens formats `strftime`)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_iso():
    """Horodatage au format persisté historique : ISO UTC + « Z »."""
    return utcnow_naive().isoformat() + "Z"


def to_iso(value):
    """Convertit un datetime (aware ou naïf) au format persisté."""
    if value is None:
        return ""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat() + "Z"


def parse_iso(value):
    """Parse un horodatage ISO (« Z » ou offset) en datetime UTC aware."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
