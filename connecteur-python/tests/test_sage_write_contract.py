"""Contrat d'écriture SFEC → Sage (`write_sfec_to_invoice`).

Régression historique : l'identifiant de table était bracket-quoté deux fois
(`[F_DOCENTETE]` puis re-quoté), ce qui faisait échouer toute écriture réelle
alors que les tests à base de mocks ne le voyaient pas. Ces tests inspectent
le SQL réellement exécuté.
"""
from contextlib import contextmanager

import pytest

from app.integration.sage import database as sage_db
from app.integration.sage.errors import PermanentSfecWriteError


class _FakeCursor:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount
        self.sql = None
        self.params = None

    def execute(self, sql, params=()):
        self.sql = " ".join(sql.split())
        self.params = tuple(params)


def _preparer(monkeypatch, *, table="F_DOCENTETE", rowcount=1):
    cursor = _FakeCursor(rowcount=rowcount)

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(sage_db, "_refresh_sfec_columns", lambda: None)
    monkeypatch.setattr(sage_db, "get_table_map",
                        lambda: {"documents": table})
    monkeypatch.setattr(sage_db, "get_cursor", fake_cursor)
    return cursor


def test_le_nom_de_table_n_est_quote_qu_une_fois(monkeypatch):
    cursor = _preparer(monkeypatch)

    resultat = sage_db.write_sfec_to_invoice(
        "0-6-FA-1", "CERT-1", "SIG", "QR", "2026-11-23T00:00:00Z")

    assert "UPDATE [F_DOCENTETE] SET" in cursor.sql
    assert "[[F_DOCENTETE]]" not in cursor.sql
    assert cursor.params[-3:] == (0, 6, "FA-1")
    assert resultat["invoice_id"] == "0-6-FA-1"


def test_schema_qualifie_est_quote_par_partie(monkeypatch):
    cursor = _preparer(monkeypatch, table="dbo.F_DOCENTETE")

    sage_db.write_sfec_to_invoice("0-6-FA-1", "C", "S", "Q", None)

    assert "UPDATE [dbo].[F_DOCENTETE] SET" in cursor.sql


def test_zero_ligne_est_une_erreur_definitive(monkeypatch):
    _preparer(monkeypatch, rowcount=0)

    with pytest.raises(PermanentSfecWriteError, match="0 ligne"):
        sage_db.write_sfec_to_invoice("0-6-FA-ABSENTE", "C", "S", "Q", None)


def test_table_invalide_ne_touche_pas_la_base(monkeypatch):
    cursor = _preparer(monkeypatch, table="F_DOCENTETE; DROP TABLE X")

    with pytest.raises(PermanentSfecWriteError):
        sage_db.write_sfec_to_invoice("0-6-FA-1", "C", "S", "Q", None)

    assert cursor.sql is None


def test_identifiant_facture_invalide(monkeypatch):
    cursor = _preparer(monkeypatch)

    with pytest.raises(PermanentSfecWriteError, match="ID facture"):
        sage_db.write_sfec_to_invoice("FA-1", "C", "S", "Q", None)

    assert cursor.sql is None
