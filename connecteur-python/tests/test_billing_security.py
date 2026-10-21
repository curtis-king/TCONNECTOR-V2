"""Tests Vague 2 facturation : validation, immuabilite, CA net et paiements partiels."""
import pytest

from app.domain import invoices as invoice_engine
from app.domain import pdf as pdf_generator
from app.storage.db import get_cursor

PREFIX = "TEST-BL-"
LINE = [{"designation": "Ligne test", "quantite": 1,
         "prix_unitaire": 1000, "taux_tva": 18}]


def _purge():
    with get_cursor() as cur:
        cur.execute("DELETE FROM invoice_lines WHERE invoice_id IN "
                    "(SELECT id FROM invoices WHERE numero LIKE ?)", (PREFIX + "%",))
        cur.execute("DELETE FROM invoices WHERE numero LIKE ?", (PREFIX + "%",))


@pytest.fixture(scope="module", autouse=True)
def _clean_billing(app_instance):
    _purge()
    yield
    _purge()


@pytest.fixture(scope="module")
def csrf(client):
    return client.get("/api/csrf").get_json()["token"]


def _post(client, csrf, payload):
    return client.post("/api/invoices", json=payload,
                       headers={"X-CSRF-Token": csrf})


def _put(client, csrf, invoice_id, payload):
    return client.put("/api/invoices/{}".format(invoice_id), json=payload,
                      headers={"X-CSRF-Token": csrf})


def test_facture_sans_ligne_refusee(client, csrf):
    r = _post(client, csrf, {"numero": PREFIX + "V-EMPTY", "tiers_nom": "X"})
    assert r.status_code == 422, r.get_json()
    assert "ligne" in r.get_json()["error"].lower()


def test_ligne_negative_ou_tva_invalide_refusee(client, csrf):
    for ligne in (
        {"designation": "x", "quantite": -1, "prix_unitaire": 1000, "taux_tva": 18},
        {"designation": "x", "quantite": 1, "prix_unitaire": 1000, "taux_tva": 7},
    ):
        r = _post(client, csrf, {"numero": PREFIX + "V-BAD",
                                 "tiers_nom": "X", "lignes": [ligne]})
        assert r.status_code == 422, r.get_json()


def test_prix_et_montant_restant_preserves_a_l_edition(client, csrf):
    r = _post(client, csrf, {"numero": PREFIX + "V-PAID", "tiers_nom": "X",
                             "statut": "valide", "lignes": LINE})
    assert r.status_code == 200, r.get_json()
    invoice_id = r.get_json()["id"]
    with get_cursor() as cur:
        cur.execute("SELECT montant_ttc FROM invoices WHERE id = ?", (invoice_id,))
        total = cur.fetchone()["montant_ttc"]
        cur.execute("UPDATE invoices SET montant_restant = ? WHERE id = ?",
                    (round(total - 500, 2), invoice_id))

    r = _put(client, csrf, invoice_id, {
        "lignes": [{"designation": "Ligne test", "quantite": 2,
                   "prix_unitaire": 1000, "taux_tva": 18}],
    })
    assert r.status_code == 200, r.get_json()
    facture = invoice_engine.get_invoice(invoice_id)
    assert 0 < facture["montant_restant"] < facture["montant_ttc"]


def test_ca_soustrait_les_avoirs(client, csrf):
    base = invoice_engine.count_invoices()["ca_total"]
    vente = _post(client, csrf, {"numero": PREFIX + "V-CA", "tiers_nom": "X",
                                 "statut": "valide", "lignes": LINE})
    assert vente.status_code == 200, vente.get_json()
    with get_cursor() as cur:
        cur.execute("UPDATE invoices SET sfec_statut = 'CERTIFIE' WHERE numero = ?",
                    (PREFIX + "V-CA",))

    avoir = _post(client, csrf, {"numero": PREFIX + "AV-CA", "type_doc": "avoir",
                                 "reference_invoice_id": PREFIX + "V-CA",
                                 "tiers_nom": "X", "statut": "valide",
                                 "lignes": [{"designation": "Retour", "quantite": 1,
                                             "prix_unitaire": 1000, "taux_tva": 0}]})
    assert avoir.status_code == 200, avoir.get_json()
    apres = invoice_engine.count_invoices()["ca_total"]
    vente_ttc = invoice_engine.get_invoice(vente.get_json()["id"])["montant_ttc"]
    avoir_ttc = invoice_engine.get_invoice(avoir.get_json()["id"])["montant_ttc"]
    assert apres == pytest.approx(base + vente_ttc - avoir_ttc, abs=0.01)


def test_facture_certifiee_non_modifiable_via_api(client, csrf):
    r = _post(client, csrf, {"numero": PREFIX + "V-LOCK", "tiers_nom": "X",
                             "statut": "valide", "lignes": LINE})
    assert r.status_code == 200, r.get_json()
    invoice_id = r.get_json()["id"]
    with get_cursor() as cur:
        cur.execute("UPDATE invoices SET sfec_statut = 'CERTIFIE' WHERE id = ?",
                    (invoice_id,))

    r = _put(client, csrf, invoice_id, {"notes": "interdit"})
    assert r.status_code == 422, r.get_json()
    r = client.delete("/api/invoices/{}".format(invoice_id),
                      headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400, r.get_json()
    assert "certifi" in r.get_json()["error"].lower()


def test_limit_invalide_ne_provoque_pas_de_500(client):
    r = client.get("/api/invoices?limit=abc&offset=-4")
    assert r.status_code == 200
    assert r.get_json()["limit"] == 200
    assert r.get_json()["offset"] == 0


def test_pdf_echappe_les_caracteres_xml(client, csrf):
    r = _post(client, csrf, {
        "numero": PREFIX + "V-PDF",
        "tiers_nom": "Client & Fils <SARL>",
        "notes": "Note <script> & test",
        "statut": "valide",
        "lignes": [{"designation": "Article & <special>", "quantite": 1,
                    "prix_unitaire": 1000, "taux_tva": 18}],
    })
    assert r.status_code == 200, r.get_json()
    facture = invoice_engine.get_invoice(r.get_json()["id"])
    contenu = pdf_generator.generate_invoice_pdf(facture)
    assert contenu.startswith(b"%PDF")


def test_stats_filtrees_refletent_les_filtres(client, csrf):
    vente = _post(client, csrf, {"numero": PREFIX + "V-SF1", "tiers_nom": "Stats",
                                 "statut": "valide", "lignes": LINE})
    assert vente.status_code == 200, vente.get_json()
    with get_cursor() as cur:
        cur.execute("UPDATE invoices SET sfec_statut = 'CERTIFIE' WHERE numero = ?",
                    (PREFIX + "V-SF1",))
    avoir = _post(client, csrf, {"numero": PREFIX + "A-SF1", "tiers_nom": "Stats",
                                 "statut": "valide", "type_doc": "avoir",
                                 "reference_invoice_id": PREFIX + "V-SF1",
                                 "lignes": LINE})
    assert avoir.status_code == 200, avoir.get_json()

    r = client.get("/api/invoices/list?search=Stats")
    body = r.get_json()
    st = body.get("stats") or {}
    assert r.status_code == 200
    assert st.get("total") >= 2
    assert st.get("certifiees") == 1

    r2 = client.get("/api/invoices/list?search=Stats&type_doc=avoir")
    st2 = r2.get_json().get("stats") or {}
    assert st2.get("total") == 1
    assert st2.get("brouillons") == 0


def test_export_csv_filtre_et_injection(client, csrf):
    from app.web.common import _csv_safe

    assert _csv_safe("=cmd") == "'=cmd"
    assert _csv_safe("@ref") == "'@ref"
    assert _csv_safe("normal") == "normal"

    r = _post(client, csrf, {"numero": PREFIX + "V-EX", "tiers_nom": "Exporta",
                             "statut": "valide", "lignes": LINE})
    assert r.status_code == 200, r.get_json()
    r = client.get("/api/invoices/export?search=Exporta")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("Content-Type", "")
    body = r.get_data(as_text=True)
    assert "numero" in body.split("\n")[0]
    assert PREFIX + "V-EX" in body
