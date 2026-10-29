"""Contrat /api/sfec/certify : ID SQLite, numero Sage et compatibilite."""
import pytest

from app.domain import invoices as invoice_engine
from app.storage import db as sqlite_db
from app.storage.db import get_cursor
from app.web.routes import config as config_routes


PREFIX = "TEST-CERT-NUM-"


def _purge():
    with get_cursor() as cur:
        cur.execute(
            "DELETE FROM invoice_lines WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE numero LIKE ?)", (PREFIX + "%",))
        cur.execute("DELETE FROM invoices WHERE numero LIKE ?", (PREFIX + "%",))


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


@pytest.fixture(autouse=True)
def _no_real_sfec_recovery(monkeypatch):
    """Les tests de contrat ne doivent pas appeler le SFEC sandbox."""
    monkeypatch.setattr(config_routes, "_local_sfec_recovery", lambda inv: None)


@pytest.fixture
def csrf(client):
    return client.get("/api/csrf").get_json()["token"]


def _create_sage_invoice(numero, statut="valide"):
    result = invoice_engine.create_invoice({
        "numero": numero,
        "source": "sage",
        "statut": statut,
        "tiers_nom": "Client Sage",
        "tiers_niu": "M0000000000000001",
        "lignes": [{
            "designation": "Ligne Sage",
            "quantite": 1,
            "prix_unitaire": 1000,
            "taux_tva": 18,
        }],
    })
    return result["id"], result["numero"]


def _post_certify(client, csrf, payload):
    return client.post(
        "/api/sfec/certify",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )


def test_certify_par_numero_sage_resout_la_facture_sqlite(
        client, csrf, monkeypatch):
    numero = PREFIX + "26FAZ07297"
    invoice_id, _ = _create_sage_invoice(numero)
    captured = []

    def fake_certify(inv):
        captured.append(inv)
        return {"success": True, "certification_number": "CERT-1"}

    monkeypatch.setattr(config_routes, "certify_sqlite_invoice", fake_certify)

    response = _post_certify(
        client, csrf, {"invoice_id": numero, "by": "numero"})

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["certification_number"] == "CERT-1"
    assert len(captured) == 1
    assert captured[0]["id"] == invoice_id
    assert captured[0]["numero"] == numero
    assert captured[0]["source"] == "sage"


def test_certify_par_inv_id_reste_compatible(client, csrf, monkeypatch):
    numero = PREFIX + "26FAZ07298"
    invoice_id, _ = _create_sage_invoice(numero)
    captured = []

    def fake_certify(inv):
        captured.append(inv)
        return {"success": True, "certification_number": "CERT-2"}

    monkeypatch.setattr(config_routes, "certify_sqlite_invoice", fake_certify)

    response = _post_certify(
        client, csrf, {"invoice_id": "INV-{}".format(invoice_id)})

    assert response.status_code == 200, response.get_json()
    assert len(captured) == 1
    assert captured[0]["id"] == invoice_id
    assert captured[0]["numero"] == numero


def test_certify_par_numero_inconnu_renvoie_404(client, csrf, monkeypatch):
    called = []
    monkeypatch.setattr(
        config_routes, "certify_sqlite_invoice",
        lambda inv: called.append(inv) or {"success": True},
    )

    response = _post_certify(
        client, csrf, {"invoice_id": PREFIX + "INEXISTANT", "by": "numero"})

    assert response.status_code == 404, response.get_json()
    assert response.get_json()["success"] is False
    assert called == []


def test_certify_par_numero_brouillon_refuse(client, csrf, monkeypatch):
    numero = PREFIX + "26FAZ07299"
    _create_sage_invoice(numero, statut="brouillon")
    monkeypatch.setattr(
        config_routes, "certify_sqlite_invoice",
        lambda inv: pytest.fail("ne doit pas certifier un brouillon"),
    )

    response = _post_certify(
        client, csrf, {"invoice_id": numero, "by": "numero"})

    assert response.status_code == 422, response.get_json()
    assert response.get_json()["success"] is False


def test_liste_billing_utilise_le_nouveau_contrat(client):
    response = client.get("/billing")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "data-invoice-numero" in html
    assert "data-invoice-source" in html
    assert "certifyBilling(this)" in html
    assert 'by:"numero"' in html


def _mark_sage_synced(invoice_id, numero):
    with get_cursor() as cur:
        cur.execute("""
            UPDATE invoices
            SET synced_sage = 1, sage_domaine = 0, sage_type = 6, sage_piece = ?
            WHERE id = ?
        """, (numero, invoice_id))


def test_certify_numero_sage_delegue_au_circuit_sage(
        client, csrf, monkeypatch):
    numero = PREFIX + "26FAZ07300"
    invoice_id, _ = _create_sage_invoice(numero)
    _mark_sage_synced(invoice_id, numero)
    captured = []

    def fake_certify_single(sage_id):
        captured.append(sage_id)
        return {"success": True, "certification_number": "CERT-SAGE"}

    monkeypatch.setattr(config_routes, "certify_single", fake_certify_single)
    monkeypatch.setattr(
        config_routes, "certify_sqlite_invoice",
        lambda inv: pytest.fail("une facture Sage doit passer par certify_single"),
    )

    response = _post_certify(
        client, csrf, {"invoice_id": numero, "by": "numero"})

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["certification_number"] == "CERT-SAGE"
    assert captured == ["0-6-" + numero]


def test_certify_deja_certifiee_ne_renvoie_pas_409(
        client, csrf, monkeypatch):
    numero = PREFIX + "26FAZ07301"
    invoice_id, _ = _create_sage_invoice(numero)
    with get_cursor() as cur:
        cur.execute("""
            UPDATE invoices
            SET sfec_statut = 'CERTIFIE', sfec_num_certif = 'CERT-EXISTANT'
            WHERE id = ?
        """, (invoice_id,))
    sqlite_db.record_sfec_anomaly(numero, "anomalie obsolete")
    monkeypatch.setattr(
        config_routes, "certify_sqlite_invoice",
        lambda inv: pytest.fail("ne doit pas recertifier une facture deja certifiee"),
    )

    response = _post_certify(
        client, csrf, {"invoice_id": numero, "by": "numero"})

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["already_certified"] is True
    assert payload["certification_number"] == "CERT-EXISTANT"
    assert sqlite_db.get_sfec_anomaly(numero) is None


def test_billing_affiche_a_surveiller_sans_certification_directe(client):
    response = client.get("/billing")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'ss==="A_SURVEILLER"' in html
    assert "A surveiller" in html
    branch = html.split('if(ss==="A_SURVEILLER")', 1)[1]
    branch = branch.split('if(ss==="EN_COURS")', 1)[0]
    assert 'href="/pending"' in branch
    assert "Certifier</button>" not in branch
