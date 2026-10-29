"""Recuperation des certifications SFEC deja existantes.

Couvre les bugs documentes dans
`docs/checking/suivi/bugs-certification-sfec-factures-deja-certifiees.md` :
- A_SURVEILLER doit etre reconsulte sur SFEC avant d'etre ignore ;
- une certification existante doit etre recuperee avant validation du payload ;
- SQLite doit etre mis a jour immediatement, sans attendre le pull Sage.
"""
import pytest

from app.storage import db as sqlite_db
from app.sync import engine


PREFIX = "TEST-ALREADY-"


def _purge():
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "DELETE FROM sfec_anomalies WHERE numero LIKE ?", (PREFIX + "%",))
        cur.execute(
            "DELETE FROM invoice_lines WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE numero LIKE ?)", (PREFIX + "%",))
        cur.execute(
            "DELETE FROM invoices WHERE numero LIKE ?", (PREFIX + "%",))


@pytest.fixture(autouse=True)
def _clean_state():
    sqlite_db.init_database()
    _purge()
    engine._retry_attempts.clear()
    engine._retry_after.clear()
    engine._logged_errors.clear()
    yield
    _purge()
    engine._retry_attempts.clear()
    engine._retry_after.clear()
    engine._logged_errors.clear()


def _insert_local(numero, statut_sfec="A_SURVEILLER"):
    with sqlite_db.get_cursor() as cur:
        cur.execute("""
            INSERT INTO invoices
                (numero, date_facture, statut, sfec_statut,
                 synced_sage, sage_domaine, sage_type, sage_piece,
                 montant_ttc, type_doc)
            VALUES (?, '2026-10-28', 'valide', ?, 1, 0, 6, ?, 1189, 'vente')
        """, (numero, statut_sfec, numero))
        return cur.lastrowid


def _sfec_invoice(numero):
    return {
        "id": "7f5c2c8e-0000-4000-8000-000000000001",
        "invoice_number": numero,
        "seller_invoice_number": numero,
        "invoice_status": "certified",
        "certification_status": "certified",
        "certification_short_signature": "CERT-ALREADY",
        "certification_signature": "SIG-ALREADY",
        "certification_qr_code": "QR-ALREADY",
        "certification_date": "2026-10-28T10:00:00Z",
    }


def test_recuperation_met_a_jour_sqlite_et_supprime_anomalie(monkeypatch):
    numero = PREFIX + "RECOVERY"
    local_id = _insert_local(numero)
    sqlite_db.record_sfec_anomaly(numero, "anomalie de test")

    monkeypatch.setattr(engine, "record_certified_invoice",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "write_sfec_to_invoice",
                        lambda **kwargs: None)

    invoice = {"id": "test08-0-6-" + numero, "numero": numero}
    recovered = engine._handle_already_certified(
        invoice, {numero: _sfec_invoice(numero)})

    assert recovered is True
    assert invoice["sfec_statut"] == "DEJA_CERTIFIE"
    assert invoice["sfec_num_certif"] == "CERT-ALREADY"

    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT * FROM invoices WHERE id = ?", (local_id,))
        row = cur.fetchone()
    assert row["sfec_statut"] == "DEJA_CERTIFIE"
    assert row["sfec_num_certif"] == "CERT-ALREADY"
    assert row["sfec_signature"] == "SIG-ALREADY"
    assert row["sfec_qr_code"] == "QR-ALREADY"
    assert row["sfec_date_certif"] == "2026-10-28T10:00:00Z"

    assert sqlite_db.get_sfec_anomaly(numero) is None


def test_auto_certify_reconsulte_les_a_surveiller(monkeypatch):
    numero = PREFIX + "AUTO"
    invoice = {
        "id": "test08-0-6-" + numero,
        "numero": numero,
        "statut_code": 2,
        "sfec_statut": "A_SURVEILLER",
        "type_doc": "vente",
        "montant_ttc": 1189,
        "date_facture": "2026-10-28",
    }
    calls = []

    monkeypatch.setitem(engine._cache, "sales_invoices", [invoice])
    monkeypatch.setitem(engine._cache, "pending_reasons", {})
    monkeypatch.setattr(engine, "_flush_pending_writes", lambda: 0)
    monkeypatch.setattr(engine, "get_config", lambda: {
        "sfec": {"enabled": True, "api_key": "test"},
        "queue": {"max_concurrent": 1},
    })
    monkeypatch.setattr(engine, "get_sfec_api_key", lambda cfg: "test")
    monkeypatch.setattr(engine, "is_online", lambda: True)
    monkeypatch.setattr(engine, "_startup_reset_done", True)
    monkeypatch.setattr(engine, "_build_sfec_lookup",
                        lambda: {numero: _sfec_invoice(numero)})
    monkeypatch.setattr(engine, "_manual_links", lambda: {})
    monkeypatch.setattr(engine, "_build_credit_note_index",
                        lambda *args, **kwargs: {})
    monkeypatch.setattr(
        engine, "_handle_already_certified",
        lambda inv, lookup=None: calls.append(inv["numero"]) or True,
    )

    engine._auto_certify_invoices_locked()

    assert calls == [numero]


def test_certify_single_recupere_avant_validation(monkeypatch):
    numero = PREFIX + "DIRECT"
    invoice = {
        "id": "test08-0-6-" + numero,
        "numero": numero,
        "statut_code": 2,
        "sfec_statut": "A_SURVEILLER",
        "type_doc": "vente",
        "montant_ttc": 1189,
        "date_facture": "2026-10-28",
    }

    class FakeClient:
        def verify_by_invoice_number(self, wanted):
            assert wanted == numero
            return _sfec_invoice(numero)

    def fake_handle(inv, lookup=None):
        inv.update({
            "sfec_statut": "DEJA_CERTIFIE",
            "sfec_num_certif": "CERT-ALREADY",
            "sfec_id": "7f5c2c8e-0000-4000-8000-000000000001",
            "sfec_signature": "SIG-ALREADY",
            "sfec_qr_code": "QR-ALREADY",
            "sfec_date_certif": "2026-10-28T10:00:00Z",
        })
        return True

    monkeypatch.setitem(engine._cache, "sales_invoices", [invoice])
    monkeypatch.setattr(engine, "get_config", lambda: {
        "sfec": {"enabled": True, "api_key": "test"},
    })
    monkeypatch.setattr(engine, "get_sfec_api_key", lambda cfg: "test")
    monkeypatch.setattr(engine, "is_online", lambda: True)
    monkeypatch.setattr(engine, "SfecClient", FakeClient)
    monkeypatch.setattr(engine, "_handle_already_certified", fake_handle)
    monkeypatch.setattr(
        engine, "certify_invoice",
        lambda inv: pytest.fail("le POST SFEC ne doit pas etre appele"),
    )

    result = engine.certify_single(invoice["id"])

    assert result["success"] is True
    assert result["already_certified"] is True
    assert result["certification_number"] == "CERT-ALREADY"
    assert result["qr_code"] == "QR-ALREADY"
