"""Validation des identifiants SQL Sage (anti-injection)."""
import pytest

from app.integration.sage import database as sage_db
from app.integration.sage.errors import SageSchemaError
from app.integration.sage.sql import quote_ident


@pytest.mark.parametrize("name,expected", [
    ("F_DOCENTETE", "[F_DOCENTETE]"),
    ("dbo_F_DOCENTETE", "[dbo_F_DOCENTETE]"),
    ("dbo.F_DOCENTETE", "[dbo].[F_DOCENTETE]"),
    ("_tmp", "[_tmp]"),
])
def test_quote_ident_valides(name, expected):
    assert quote_ident(name) == expected


@pytest.mark.parametrize("name", [
    "", None, 42, "F_DOC; DROP TABLE X", "F_DOCENTETE--", "[x]",
    "dbo..F_DOCENTETE", "a b", "F_DOCENTETE;", "1TABLE",
])
def test_quote_ident_invalides(name):
    with pytest.raises(SageSchemaError):
        quote_ident(name)


def test_ecriture_refuse_une_table_non_valide(monkeypatch):
    monkeypatch.setattr(sage_db, "_refresh_sfec_columns", lambda: None)
    monkeypatch.setattr(
        sage_db, "get_table_map",
        lambda: {"documents": "F_DOCENTETE; DROP TABLE X"},
    )
    with pytest.raises(sage_db.PermanentSfecWriteError):
        sage_db.write_sfec_to_invoice("0-6-F1", "CERT1", "SIG", "QR", "2026-11-23")
