"""Coherence du statut SFEC entre SQLite, la file pending et les pages.

Une certification SFEC valide ne doit pas disparaitre parce que l'ecriture
Sage est encore bloquee par un verrou SQL Server.
"""
import pytest

from app.integration.sage import writer
from app.integration.sfec import pending_writes
from app.storage import db as sqlite_db
from app.web.routes import dashboard

PREFIX = "TEST-STATUS-"


def _cleanup():
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "DELETE FROM invoice_lines WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE numero LIKE ?)",
            (PREFIX + "%",),
        )
        cur.execute("DELETE FROM invoices WHERE numero LIKE ?", (PREFIX + "%",))
        cur.execute(
            "DELETE FROM sfec_pending_writes WHERE invoice_id LIKE ?",
            ("test-status-%",),
        )


@pytest.fixture(autouse=True)
def _clean_state():
    sqlite_db.init_database()
    pending_writes.init_pending_table()
    _cleanup()
    yield
    _cleanup()


def _insert_invoice(numero, *, sfec_statut="", sfec_num_certif="",
                    sfec_signature="", sfec_qr_code="",
                    sfec_date_certif="", sfec_id=""):
    with sqlite_db.get_cursor() as cur:
        cur.execute("""
            INSERT INTO invoices (
                numero, date_facture, statut, valide, source, tiers_nom,
                montant_ttc, sfec_statut, sfec_num_certif, sfec_signature,
                sfec_qr_code, sfec_date_certif, sfec_id
            ) VALUES (?, '2026-10-29', 'valide', 1, 'sage', 'Test statut',
                      1000, ?, ?, ?, ?, ?, ?)
        """, (
            numero, sfec_statut, sfec_num_certif, sfec_signature,
            sfec_qr_code, sfec_date_certif, sfec_id,
        ))
        return cur.lastrowid


def _pending_invoice_id(numero):
    return "test-status-0-6-" + numero


def _mirror_fields(_invoice):
    return {
        "type_doc": "vente",
        "sage_type": 6,
        "sage_domaine": 0,
        "sage_piece": "PIECE",
        "montant_tva": 0,
        "montant_ht_brut": 1000,
        "total_tax_t_amount": 0,
        "total_tax_r_amount": 0,
        "total_exempt_amount": 0,
        "discount_amount": 0,
        "total_line_discount_amount": 0,
        "additional_cent_tax": 0,
        "electronic_stamp_duty": 0,
    }


def _sage_pull(numero, statut="A_SURVEILLER"):
    return {
        "numero": numero,
        "date_facture": "2026-10-29",
        "statut": "A COMPTABILISER",
        "valide": True,
        "tiers_nom": "Test statut",
        "montant_ht": 1000,
        "montant_ttc": 1000,
        "montant_restant": 1000,
        "sfec_statut": statut,
        "sfec_num_certif": "",
        "sfec_signature": "",
        "sfec_qr_code": "",
    }


def _fetch_invoice(numero):
    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT * FROM invoices WHERE numero = ?", (numero,))
        return cur.fetchone()


def test_pull_sage_vide_ne_reecrase_pas_certification_locale(monkeypatch):
    numero = PREFIX + "F1"
    _insert_invoice(
        numero,
        sfec_statut="CERTIFIE",
        sfec_num_certif="CERT-LOCAL",
        sfec_signature="SIG-LOCAL",
        sfec_qr_code="QR-LOCAL",
        sfec_date_certif="2026-10-29T06:13:43.01Z",
        sfec_id="93e873b3-186a-4498-9ab5-99842be826a9",
    )
    monkeypatch.setattr(
        "app.sync.bidirectional._mirror_fields", _mirror_fields,
    )

    writer.sync_sage_invoice_to_sqlite(_sage_pull(numero))

    row = _fetch_invoice(numero)
    assert row["sfec_statut"] == "CERTIFIE"
    assert row["sfec_num_certif"] == "CERT-LOCAL"
    assert row["sfec_signature"] == "SIG-LOCAL"
    assert row["sfec_qr_code"] == "QR-LOCAL"
    assert row["sfec_date_certif"] == "2026-10-29T06:13:43.01Z"


def test_pull_sage_vide_hydrate_depuis_pending(monkeypatch):
    numero = PREFIX + "F2"
    invoice_id = _pending_invoice_id(numero)
    _insert_invoice(
        numero,
        sfec_statut="A_SURVEILLER",
        sfec_date_certif="2026-10-29T06:13:43.01Z",
        sfec_id="93e873b3-186a-4498-9ab5-99842be826a9",
    )
    pending_writes.enqueue(
        invoice_id, "CERT-PENDING", "SIG-PENDING", "QR-PENDING",
        "2026-10-29T06:13:43.01Z",
    )
    monkeypatch.setattr(
        "app.sync.bidirectional._mirror_fields", _mirror_fields,
    )

    writer.sync_sage_invoice_to_sqlite(_sage_pull(numero))

    row = _fetch_invoice(numero)
    assert row["sfec_statut"] == "CERTIFIE"
    assert row["sfec_num_certif"] == "CERT-PENDING"
    assert row["sfec_signature"] == "SIG-PENDING"
    assert row["sfec_qr_code"] == "QR-PENDING"
    assert row["sfec_date_certif"] == "2026-10-29T06:13:43.01Z"


def test_api_invoices_list_expose_certification_en_attente(client):
    numero = PREFIX + "F3"
    invoice_id = _pending_invoice_id(numero)
    _insert_invoice(
        numero,
        sfec_statut="A_SURVEILLER",
        sfec_id="93e873b3-186a-4498-9ab5-99842be826a9",
    )
    pending_writes.enqueue(
        invoice_id, "CERT-API", "SIG-API", "QR-API",
        "2026-10-29T06:13:43.01Z",
    )

    response = client.get("/api/invoices/list?search=" + numero)

    assert response.status_code == 200
    data = response.get_json()
    invoice = next(
        item for item in data["invoices"] if item["numero"] == numero
    )
    assert invoice["sfec_statut"] == "CERTIFIE"
    assert invoice["sfec_num_certif"] == "CERT-API"
    assert invoice["sfec_pending_sage"] is True


def test_page_invoices_affiche_certifie_sfec_en_attente(client, monkeypatch):
    numero = PREFIX + "F4"
    invoice_id = _pending_invoice_id(numero)
    _insert_invoice(
        numero,
        sfec_statut="A_SURVEILLER",
        sfec_id="93e873b3-186a-4498-9ab5-99842be826a9",
    )
    pending_writes.enqueue(
        invoice_id, "CERT-PAGE", "SIG-PAGE", "QR-PAGE",
        "2026-10-29T06:13:43.01Z",
    )
    cache = {
        "sales_invoices": [{
            "id": invoice_id,
            "numero": numero,
            "type_doc": "vente",
            "statut": "COMPTABILISE",
            "statut_code": 2,
            "valide": 1,
            "sfec_statut": "A_SURVEILLER",
            "date_facture": "2026-10-29",
            "nom_tiers": "Test statut",
            "montant_ttc": 1000,
        }],
        "sfec_invoices": [],
    }
    monkeypatch.setattr(dashboard, "get_cache", lambda: cache)

    response = client.get("/invoices?type=sale")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Certifiée SFEC — Sage en attente" in html
    assert numero in html
