"""Tests Vague 2 POS : validation des lignes, tiers fiscaux, atomicite et immuabilite."""
import uuid

import pytest

from app.domain import invoices as invoice_engine
from app.domain import pos as pos_engine
from app.storage.db import get_cursor

PREFIX = "TEST-POS-"


def _purge():
    with get_cursor() as cur:
        cur.execute(
            "SELECT invoice_id FROM pos_tickets WHERE numero LIKE ?",
            (PREFIX + "%",))
        invoice_ids = [r["invoice_id"] for r in cur.fetchall() if r["invoice_id"]]
        cur.execute("DELETE FROM pos_ticket_lines WHERE ticket_id IN "
                    "(SELECT id FROM pos_tickets WHERE numero LIKE ?)",
                    (PREFIX + "%",))
        cur.execute("DELETE FROM pos_tickets WHERE numero LIKE ?", (PREFIX + "%",))
        for invoice_id in invoice_ids:
            cur.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (invoice_id,))
            cur.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
        cur.execute("DELETE FROM invoices WHERE tiers_code LIKE ?", (PREFIX + "%",))


@pytest.fixture(scope="module", autouse=True)
def _clean_pos_data(app_instance):
    _purge()
    yield
    _purge()


def _ticket(**kwargs):
    data = {
        "numero": PREFIX + "T1",
        "tiers_code": PREFIX + "C1",
        "tiers_nom": "Client POS test",
        "tiers_type": "individual",
        "mode_paiement": "virement",
        "lignes": [{"designation": "Article test", "quantite": 1,
                    "prix_unitaire": 1000, "taux_tva": 18}],
    }
    data.update(kwargs)
    return data


def test_lignes_invalides_refusees():
    for ligne in (
        {"designation": "x", "quantite": 0, "prix_unitaire": 1000, "taux_tva": 18},
        {"designation": "x", "quantite": 1, "prix_unitaire": -1, "taux_tva": 18},
        {"designation": "x", "quantite": "abc", "prix_unitaire": 1000, "taux_tva": 18},
        {"designation": "x", "quantite": 1, "prix_unitaire": 1000, "taux_tva": 7},
    ):
        result = pos_engine.create_ticket(_ticket(lignes=[ligne]))
        assert result["success"] is False, result


def test_mode_et_montant_especes_valides():
    result = pos_engine.create_ticket(_ticket(mode_paiement="autre"))
    assert result["success"] is False

    result = pos_engine.create_ticket(_ticket(mode_paiement="especes", montant_recu=100))
    assert result["success"] is False
    assert "insuffisant" in result["error"].lower()


def test_b2b_sans_niu_refuse():
    result = pos_engine.create_ticket(_ticket(tiers_type="business", tiers_niu=""))
    assert result["success"] is False
    assert "niu" in result["error"].lower()


def test_b2b_avec_niu_cree_facture_et_champs_fiscaux():
    niu = "CG123456789AA45AA"
    result = pos_engine.create_ticket(_ticket(
        numero=PREFIX + "T-B2B",
        tiers_type="business",
        tiers_niu=niu,
        tiers_rccm="RCCM-TEST-1",
        is_recipient_taxable=1,
    ))
    assert result["success"] is True, result
    assert result["invoice_id"]

    facture = invoice_engine.get_invoice(result["invoice_id"])
    assert facture["tiers_niu"] == niu
    assert facture["tiers_type"] == "business"
    assert facture["recipient_rccm"] == "RCCM-TEST-1"
    assert facture["is_recipient_taxable"] == 1
    assert facture["source"] == "pos"


def test_facture_certifiee_non_modifiable_ni_supprimable():
    result = pos_engine.create_ticket(_ticket(numero=PREFIX + "T-LOCK"))
    assert result["success"] is True, result
    invoice_id = result["invoice_id"]

    with get_cursor() as cur:
        cur.execute("UPDATE invoices SET sfec_statut = 'CERTIFIE' WHERE id = ?",
                    (invoice_id,))

    with pytest.raises(invoice_engine.InvoiceIntegrityError):
        invoice_engine.update_invoice(invoice_id, {"notes": "modification interdite"})
    with pytest.raises(invoice_engine.InvoiceIntegrityError):
        invoice_engine.delete_invoice(invoice_id)


# ── Idempotence de creation de ticket (PO-25/PO-63) ─────────────────────────

def test_ticket_idempotent_sur_retry():
    """Un retry avec la meme cle d'idempotence ne duplique ni le ticket,
    ni la facture : meme ticket_id, flag duplicate."""
    key = "test-idem-{}".format(uuid.uuid4().hex[:16])
    r1 = pos_engine.create_ticket(_ticket(idempotency_key=key))
    assert r1["success"] is True, r1
    r2 = pos_engine.create_ticket(_ticket(idempotency_key=key))
    assert r2["success"] is True, r2
    assert r2.get("duplicate") is True
    assert r2["id"] == r1["id"]
    assert r2["numero"] == r1["numero"]
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM pos_tickets WHERE id = ?", (r1["id"],))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT COUNT(*) AS n FROM pos_idempotency WHERE key = ?", (key,))
        assert cur.fetchone()["n"] == 1
        cur.execute("DELETE FROM pos_idempotency WHERE key = ?", (key,))


def test_cle_idempotence_invalide_refusee():
    result = pos_engine.create_ticket(_ticket(idempotency_key="!!bad key!!"))
    assert result["success"] is False
    assert "idempotence" in result["error"].lower()
