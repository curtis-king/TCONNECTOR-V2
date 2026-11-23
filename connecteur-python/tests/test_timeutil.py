"""Horodatages UTC : format persisté inchangé, parsing tolérant."""
from datetime import datetime, timedelta, timezone

from app.core.timeutil import parse_iso, to_iso, utcnow, utcnow_iso, utcnow_naive


def test_utcnow_est_aware():
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_utcnow_naive_est_utc_sans_tzinfo():
    now = utcnow_naive()
    assert now.tzinfo is None
    delta = abs((datetime.now(timezone.utc).replace(tzinfo=None) - now).total_seconds())
    assert delta < 2


def test_utcnow_iso_format_historique():
    value = utcnow_iso()
    assert value.endswith("Z")
    # Même format que l'ancien datetime.utcnow().isoformat() + "Z".
    parsed = datetime.fromisoformat(value[:-1])
    assert parsed.tzinfo is None


def test_to_iso_convertit_en_utc():
    paris = timezone(timedelta(hours=1))
    value = to_iso(datetime(2026, 1, 2, 10, 0, 0, tzinfo=paris))
    assert value == "2026-01-02T09:00:00Z"
    assert to_iso(None) == ""


def test_parse_iso_tolere_z_offset_et_naif():
    aware = parse_iso("2026-11-23T12:00:00Z")
    assert aware == datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)

    offset = parse_iso("2026-11-23T13:00:00+01:00")
    assert offset == aware

    naive = parse_iso("2026-11-23T12:00:00")
    assert naive == aware

    assert parse_iso("") is None
    assert parse_iso("pas une date") is None
