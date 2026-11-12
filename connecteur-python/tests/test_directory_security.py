"""Tests Vague 3 annuaire : validation, references, PIN, comptes et audit."""
import pytest

from app.storage.db import get_cursor

PREFIX = "TEST-DIR-"
LINE = [{"designation": "Ligne test", "quantite": 1,
         "prix_unitaire": 1000, "taux_tva": 0}]


def _purge():
    with get_cursor() as cur:
        cur.execute("DELETE FROM invoice_lines WHERE invoice_id IN "
                    "(SELECT id FROM invoices WHERE numero LIKE ?)", (PREFIX + "%",))
        cur.execute("DELETE FROM invoices WHERE numero LIKE ?", (PREFIX + "%",))
        cur.execute("DELETE FROM contacts WHERE code LIKE ?", (PREFIX + "%",))
        cur.execute("DELETE FROM vendeurs WHERE code LIKE ?", (PREFIX + "%",))
        cur.execute("DELETE FROM utilisateurs WHERE email LIKE ?",
                    ("test-dir-%",))


@pytest.fixture(scope="module", autouse=True)
def _clean_directory(app_instance):
    _purge()
    yield
    _purge()


@pytest.fixture(scope="module")
def csrf(client):
    return client.get("/api/csrf").get_json()["token"]


def _post(client, csrf, path, payload):
    return client.post(path, json=payload, headers={"X-CSRF-Token": csrf})


def test_contact_niu_invalide_refuse(client, csrf):
    r = _post(client, csrf, "/api/contacts", {
        "code": PREFIX + "C-BAD", "nom": "Bad", "niu": "abc"})
    assert r.status_code == 400, r.get_json()
    assert "niu" in r.get_json()["error"].lower()


def test_contact_email_invalide_refuse(client, csrf):
    r = _post(client, csrf, "/api/contacts", {
        "code": PREFIX + "C-MAIL", "nom": "Bad", "email": "pas-un-email"})
    assert r.status_code == 400, r.get_json()
    assert "email" in r.get_json()["error"].lower()


def test_total_contacts_reflete_la_base_pas_la_page(client):
    for i in range(3):
        _post(client, client.get("/api/csrf").get_json()["token"], "/api/contacts", {
            "code": PREFIX + "C-T{}".format(i), "nom": "Contact {}".format(i)})
    r = client.get("/api/contacts?limit=1")
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["contacts"]) == 1
    assert body["total"] >= 3


def test_suppression_contact_reference_refusee(client, csrf):
    contact = _post(client, csrf, "/api/contacts", {
        "code": PREFIX + "C-REF", "nom": "Contact reference"})
    contact_id = contact.get_json()["id"]
    invoice = _post(client, csrf, "/api/invoices", {
        "numero": PREFIX + "V-REF", "tiers_nom": "Contact reference",
        "contact_id": contact_id, "lignes": LINE})
    assert invoice.status_code == 200, invoice.get_json()

    r = client.delete("/api/contacts/{}".format(contact_id),
                      headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409, r.get_json()
    assert "desactivez" in r.get_json()["error"].lower()


def test_api_vendeurs_ne_divulgue_pas_le_pin(client, csrf):
    r = _post(client, csrf, "/api/vendeurs", {
        "code": PREFIX + "V1", "nom": "Vendeur", "pin": "1234"})
    assert r.status_code == 200, r.get_json()
    vendeur_id = r.get_json()["id"]

    liste = client.get("/api/vendeurs").get_json()["vendeurs"]
    vendeur = next(v for v in liste if v["id"] == vendeur_id)
    assert vendeur["pin"] == "***"
    detail = client.get("/api/vendeurs/{}".format(vendeur_id)).get_json()
    assert detail["pin"] == "***"


def test_admin_ne_peut_pas_se_desactiver(role_client):
    admin = role_client("admin")
    me = admin.get("/api/utilisateurs").get_json()["comptes"]
    own = next(u for u in me if u["email"] == "test-admin@example.com")
    r = admin.put("/api/utilisateurs/{}".format(own["id"]),
                  json={"toggle_actif": True},
                  headers={"X-CSRF-Token": admin.get("/api/csrf").get_json()["token"]})
    assert r.status_code == 400, r.get_json()
    assert "propre" in r.get_json()["error"].lower()


def test_parametres_securite_invalides_refuses(client, csrf):
    r = _post(client, csrf, "/api/auth/config", {
        "provider": "inconnu", "max_attempts": 0})
    assert r.status_code == 400, r.get_json()


def test_caissiere_peut_consulter_mais_pas_modifier_annuaire(role_client):
    client = role_client("caissiere")
    token = client.get("/api/csrf").get_json()["token"]
    assert client.get("/clients").status_code == 200
    assert client.get("/vendeurs").status_code == 200
    assert client.put("/api/contacts/1", json={"nom": "x"},
                      headers={"X-CSRF-Token": token}).status_code == 403
    assert client.delete("/api/contacts/1",
                         headers={"X-CSRF-Token": token}).status_code == 403
    assert client.put("/api/vendeurs/1", json={"nom": "x"},
                      headers={"X-CSRF-Token": token}).status_code == 403
    assert client.delete("/api/vendeurs/1",
                         headers={"X-CSRF-Token": token}).status_code == 403


def test_recherche_et_pagination_serveur_contacts(client, csrf):
    for i in range(3):
        _post(client, csrf, "/api/contacts", {
            "code": PREFIX + "PAGE{}".format(i),
            "nom": "Pagination {}".format(i)})
    r = client.get("/api/contacts?search=Pagination&limit=2&offset=2")
    body = r.get_json()
    assert r.status_code == 200
    assert body["total"] == 3
    assert len(body["contacts"]) == 1
    assert body["offset"] == 2


def test_creation_compte_refuse_mot_de_passe_faible(client, csrf):
    r = _post(client, csrf, "/api/utilisateurs", {
        "nom": "Faible", "email": "test-dir-faible@example.com",
        "role": "caissiere", "password": "abcdefghij"})
    assert r.status_code == 400
    assert "lettre" in r.get_json()["error"].lower()


def test_audit_page_paginee(client, csrf):
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM audit_log")
        n0 = cur.fetchone()["n"]
        for i in range(110):
            cur.execute(
                "INSERT INTO audit_log (utilisateur, action, detail, ip) "
                "VALUES (?, ?, ?, ?)",
                ("TEST-DIR", "test.pag.{}".format(i), "l{}".format(i), "127.0.0.1"))
    r1 = client.get("/utilisateurs")
    assert r1.status_code == 200
    body1 = r1.get_data(as_text=True)
    assert "Page 1 /" in body1 and "/utilisateurs?page=2" in body1
    # La premiere page est bornee a 50 lignes.
    assert body1.count("test.pag.") == 50
    r2 = client.get("/utilisateurs?page=2")
    assert r2.status_code == 200
    body2 = r2.get_data(as_text=True)
    assert "Page 2 /" in body2 and "Prec" in body2
    # Purge des lignes de test pour ne pas fausser d'autres assertions.
    with get_cursor() as cur:
        cur.execute("DELETE FROM audit_log WHERE action LIKE 'test.pag.%'")
        cur.execute("SELECT COUNT(*) AS n FROM audit_log")
        assert cur.fetchone()["n"] == n0
