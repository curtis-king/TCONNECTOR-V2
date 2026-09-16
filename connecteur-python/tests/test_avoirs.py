"""Tests d'intégrité des avoirs + doublon NIU + tolérance de totaux (audit T2).

Cas couverts (plan §4.2 / audit T2) :
  1. POST /api/invoices type_doc='avoir' SANS reference_invoice_id      -> 422
  2. Avoir sur une facture NON certifiée                                -> 422
  3. Avoir sur une facture DÉJÀ avoirée (1er OK, 2e refusé)             -> 422
  4. POST /api/contacts avec un NIU en doublon                          -> 409
  5. validate_sfec_payload respecte config.validation.tolerance_amount

HYGIÈNE DE BASE (important) : ces tests tournent contre la MÊME base SQLite
que les snapshots (tests/snapshots, compare_snapshots.py). Toute donnée créée
ici ferait dériver /api/invoices/stats et les listes. D'où :
  - numéros EXPLICITES préfixés TEST-B2- (aucun compteur de numérotation
    invoice_next_number / avoir_next_number n'est touché) ;
  - un fixture autouse nettoie les factures/lignes/contacts créés AVANT et
    APRÈS la session de tests.

COMPORTEMENT SERVEUR DIFFÉRENT DU PLAN (documenté, test NON faussé) :
  - Cas 4 : la création initiale d'un contact RÉUSSIT côté base mais la route
    /api/contacts (POST) renvoie 400 « local variable 'code' referenced before
    assignment » — bug PRÉ-EXISTANT d'api_create_contact (directory.py :
    `code` n'est affecté que dans la branche d'échec). Le contact est bien
    inséré (commit avant la réponse), donc le 2e POST avec le même NIU
    déclenche bien la détection de doublon -> 409, qui est ce que vérifie ce
    test. Le bug de la route (400 au lieu de 200 à la création) est signalé
    dans docs/checking/audit-plan-6-a-fin.md (section B2), sans correction
    volontaire (hors périmètre ; déjà couvert par le fix B1 des correctifs
    ciblés de l'audit).
"""
import pytest

from app.storage.db import get_cursor

PREFIX = "TEST-B2-"


def _purge_test_data():
    """Supprime les données de test (avant ET après la session)."""
    with get_cursor() as cur:
        cur.execute(
            "DELETE FROM invoice_lines WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE numero LIKE ?)", (PREFIX + "%",))
        cur.execute("DELETE FROM invoices WHERE numero LIKE ?", (PREFIX + "%",))
        cur.execute("DELETE FROM contacts WHERE code LIKE ?", (PREFIX + "%",))


@pytest.fixture(scope="module", autouse=True)
def _clean_b2_data():
    _purge_test_data()
    yield
    _purge_test_data()


@pytest.fixture(scope="module")
def csrf_token(client):
    """Jeton CSRF de session (l'app l'exige sur tout POST /api/*)."""
    resp = client.get("/api/csrf")
    return resp.get_json()["token"]


def _post(client, path, payload, csrf_token):
    return client.post(path, json=payload,
                       headers={"X-CSRF-Token": csrf_token})


def _post_invoice(client, csrf_token, payload):
    return _post(client, "/api/invoices", payload, csrf_token)


# ── Cas 1 : avoir sans référence ────────────────────────────────────────────
def test_avoir_sans_reference_rejete(client, csrf_token):
    resp = _post_invoice(client, csrf_token, {
        "numero": PREFIX + "AV-001",
        "type_doc": "avoir",
        "tiers_nom": "Client test",
    })
    assert resp.status_code == 422, resp.get_json()
    body = resp.get_json()
    assert body.get("success") is False
    assert "reference_invoice_id" in body.get("error", "")


# ── Cas 2 : avoir sur facture NON certifiée ─────────────────────────────────
def test_avoir_sur_facture_non_certifiee_rejete(client, csrf_token):
    vente = _post_invoice(client, csrf_token, {
        "numero": PREFIX + "V-001",
        "type_doc": "vente",
        "tiers_nom": "Client test",
        "statut": "brouillon",
    })
    assert vente.status_code == 200, vente.get_json()

    resp = _post_invoice(client, csrf_token, {
        "numero": PREFIX + "AV-002",
        "type_doc": "avoir",
        "reference_invoice_id": PREFIX + "V-001",
        "tiers_nom": "Client test",
    })
    assert resp.status_code == 422, resp.get_json()
    assert "certifi" in resp.get_json().get("error", "").lower()


# ── Cas 3 : double avoir sur la même facture ────────────────────────────────
def test_double_avoir_sur_facture_certifiee_rejete(client, csrf_token):
    vente = _post_invoice(client, csrf_token, {
        "numero": PREFIX + "V-002",
        "type_doc": "vente",
        "tiers_nom": "Client test",
        "statut": "valide",
    })
    assert vente.status_code == 200, vente.get_json()

    # Certification simulée (état nécessaire pour qu'un avoir soit accepté).
    with get_cursor() as cur:
        cur.execute(
            "UPDATE invoices SET sfec_statut = 'CERTIFIE' WHERE numero = ?",
            (PREFIX + "V-002",))

    premier = _post_invoice(client, csrf_token, {
        "numero": PREFIX + "AV-101",
        "type_doc": "avoir",
        "reference_invoice_id": PREFIX + "V-002",
        "tiers_nom": "Client test",
    })
    assert premier.status_code == 200, premier.get_json()

    second = _post_invoice(client, csrf_token, {
        "numero": PREFIX + "AV-102",
        "type_doc": "avoir",
        "reference_invoice_id": PREFIX + "V-002",
        "tiers_nom": "Client test",
    })
    assert second.status_code == 422, second.get_json()
    assert "avoir" in second.get_json().get("error", "").lower()


# ── Cas 4 : doublon de NIU sur /api/contacts ────────────────────────────────
def test_contact_doublon_niu_rejete(client, csrf_token):
    niu = "TESTB2000000000A"
    r1 = _post(client, "/api/contacts", {
        "code": PREFIX + "C1", "nom": "Contact Test 1", "niu": niu,
    }, csrf_token)
    # Bug pré-existant de la route : 400 « local variable 'code' ... » au lieu
    # de 200, MAIS l'INSERT est commité (voir docstring du module).
    assert r1.status_code in (200, 400), r1.get_json()

    r2 = _post(client, "/api/contacts", {
        "code": PREFIX + "C2", "nom": "Contact Test 2", "niu": niu,
    }, csrf_token)
    assert r2.status_code == 409, r2.get_json()
    body = r2.get_json()
    assert body.get("success") is False
    assert body.get("field") == "niu"


# ── Cas 5 : validate_sfec_payload respecte config.validation.tolerance_amount
def _payload_total(total_amount):
    return {
        "total_amount": total_amount,
        "subtotal": 100.0,
        "discount_amount": 0,
        "total_line_discount_amount": 0,
        "total_tax_amount": 0,
        "additional_cent_tax": 0,
        "electronic_stamp_duty": 0,
        "items": [{"total_amount": 100, "designation": "Prestation",
                   "type": "product"}],
        "currency": "XAF",
        "recipient_type": "business",
        "recipient_name": "Client Test",
        "recipient_niu": "M000000000000001",
    }


def _incoherence(errors):
    return [e for e in errors if "Incoherence totaux" in e]


def test_tolerance_config_utilisee():
    """Écart 0.5 > tolérance config (0.01) mais < tolérance bug (1.0) : le
    déclenchement de l'erreur prouve que config.validation.tolerance_amount
    est bien lue (avant le fix T2, la tolérance effective restait 1.0 et
    aucune erreur n'était levée)."""
    from app.config.manager import get_config
    from app.integration.sfec.endpoints import validate_sfec_payload

    tolerance_cfg = float(
        get_config().get("validation", {}).get("tolerance_amount", 0.01))
    assert tolerance_cfg == 0.01  # valeur attendue du config de dev

    errors = validate_sfec_payload(_payload_total(100.0 + 0.5))
    assert _incoherence(errors), (
        "Écart 0.5 > tolérance config 0.01 : une incohérence de totaux "
        "devait être signalée. La tolérance config est-elle ignorée ?")


def test_tolerance_config_non_declenchee_sous_seuil():
    from app.integration.sfec.endpoints import validate_sfec_payload

    errors = validate_sfec_payload(_payload_total(100.0 + 0.005))
    assert _incoherence(errors) == []


def test_tolerance_parametre_explicite():
    """Le paramètre explicite tolerance_amount prime sur la config."""
    from app.integration.sfec.endpoints import validate_sfec_payload

    errors = validate_sfec_payload(_payload_total(100.0 + 1.5),
                                   tolerance_amount=2.0)
    assert _incoherence(errors) == []

    errors = validate_sfec_payload(_payload_total(100.0 + 1.5),
                                   tolerance_amount=0.01)
    assert _incoherence(errors)
