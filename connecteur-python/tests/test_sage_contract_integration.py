"""Contrat de l'adaptateur Sage sur une base réelle (opt-in).

Ces tests ne s'exécutent que si `TCONNECTOR_TEST_REAL_SAGE=1` **et** si on
demande explicitement le marqueur `integration` :

    TCONNECTOR_TEST_REAL_SAGE=1 pytest -m integration tests/test_sage_contract_integration.py

Ils sont strictement en lecture seule, à une exception près : l'écriture SFEC
est appelée sur un identifiant de facture inexistant, donc l'UPDATE porte sur
0 ligne et lève `PermanentSfecWriteError` sans modifier Sage. Aucun DDL n'est
déclenché (le rafraîchissement des colonnes est neutralisé).
"""
import os

import pytest

if os.environ.get("TCONNECTOR_TEST_REAL_SAGE") != "1":
    pytest.skip(
        "Sage réel non activé (TCONNECTOR_TEST_REAL_SAGE=1 requis)",
        allow_module_level=True,
    )

from app.integration.sage import connection as sage_conn  # noqa: E402
from app.integration.sage import database as sage_db  # noqa: E402
from app.integration.sage import schema as sage_schema  # noqa: E402
from app.integration.sage.errors import PermanentSfecWriteError  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def _fermer_pool():
    yield
    sage_conn.close_pool()


def test_ping_et_table_map():
    resultat = sage_conn.ping_database()

    assert resultat["ok"] is True, resultat
    table_map = sage_schema.get_table_map()
    assert table_map["documents"]
    assert table_map["document_lines"]
    assert table_map["third_party"]


def test_decouverte_du_schema_documents():
    table_map = sage_schema.get_table_map()
    colonnes = sage_schema._TABLE_COLUMNS[table_map["documents"].upper()]

    for colonne in ("DO_DOMAINE", "DO_TYPE", "DO_PIECE"):
        assert colonne in colonnes


def test_lectures_ventes_et_achats():
    ventes = sage_db.fetch_sales_invoices(limit=2)
    achats = sage_db.fetch_purchase_invoices(limit=2)

    assert all(inv["id"].count("-") >= 2 for inv in ventes)
    assert all(inv["id"].count("-") >= 2 for inv in achats)


def test_references_lues():
    assert sage_db.fetch_tax_rates()
    assert sage_db.fetch_ledger_accounts()


def test_ecriture_sur_facture_absente_est_definitive(monkeypatch):
    """0 ligne impactée : l'erreur est définitive, jamais rejouée en boucle."""
    monkeypatch.setattr(sage_db, "_refresh_sfec_columns", lambda: None)

    with pytest.raises(PermanentSfecWriteError, match="0 ligne"):
        sage_db.write_sfec_to_invoice(
            invoice_id="999999-99-FACTURE-ABSENTE",
            certification_number="CERT-TEST",
            signature="SIG",
            qr_code="QR",
            certification_date="2026-11-23T00:00:00Z",
        )
