"""Traitement automatique du NIU a la certification SFEC.

Regle retenue : un client comptoir / anonyme est un particulier sans NIU.
Aucune donnee client n'est modifiee : la decision est prise au moment de
construire le payload SFEC.
"""
import pytest

from app.core.utils import is_counter_customer
from app.domain import pos as pos_engine
from app.integration.sfec import endpoints as sfec_endpoints
from app.integration.sfec.endpoints import (
    db_invoice_to_sfec,
    sqlite_invoice_to_sfec,
    validate_sfec_payload,
)
from app.storage import db as sqlite_db


def _line(**kw):
    line = {
        "designation": "Produit",
        "quantite": 1,
        "prix_unitaire": 1000,
        "montant_ht": 1000,
        "taux_tva": 18,
        "montant_tva": 180,
        "montant_ttc": 1189,
    }
    line.update(kw)
    return line


def _invoice(**kw):
    inv = {
        "numero": "FA-NIU-1",
        "type_doc": "vente",
        "reference": "",
        "nom_tiers": "Client Test",
        "recipient_niu": "M0000000000000001",
        "recipient_type_raw": "business",
        "montant_ht": 1000,
        "montant_tva": 180,
        "montant_ttc": 1189,
        "montant_restant": 1189,
        "lignes": [_line()],
    }
    inv.update(kw)
    return inv


def test_client_comptoir_code_sans_niu_est_individual():
    payload = db_invoice_to_sfec(_invoice(
        tiers_code="CLI-CPT",
        nom_tiers="Client comptoir",
        recipient_type_raw="business",
        recipient_niu="",
    ))

    assert payload["recipient_type"] == "individual"
    assert "recipient_niu" not in payload
    assert validate_sfec_payload(payload) == []


def test_client_comptoir_nom_sans_niu_est_individual():
    payload = db_invoice_to_sfec(_invoice(
        tiers_code="AUTRE-CODE",
        nom_tiers="Clients Comptant ALUCONGO K BZV",
        recipient_type_raw="business",
        recipient_niu="",
    ))

    assert payload["recipient_type"] == "individual"
    assert "recipient_niu" not in payload


def test_code_tiers_sage_est_reconnu_sans_alias():
    payload = db_invoice_to_sfec(_invoice(
        tiers_code=None,
        code_tiers="CLI-CPT",
        nom_tiers="Client comptoir",
        recipient_type_raw="business",
        recipient_niu="",
    ))

    assert payload["recipient_type"] == "individual"
    assert "recipient_niu" not in payload


def test_entreprise_sans_niu_reste_refusee():
    payload = db_invoice_to_sfec(_invoice(
        tiers_code="BAGUES",
        nom_tiers="Bague's en or",
        recipient_type_raw="business",
        recipient_niu="",
    ))

    assert payload["recipient_type"] == "business"
    assert any("recipient_niu requis" in error
               for error in validate_sfec_payload(payload))


def test_niu_invalide_nest_pas_remplace():
    payload = db_invoice_to_sfec(_invoice(
        tiers_code="BAGUES",
        nom_tiers="Bague's en or",
        recipient_type_raw="business",
        recipient_niu="546789",
    ))

    assert payload["recipient_niu"] == "546789"
    assert any("recipient_niu doit contenir 16 ou 17 caracteres" in error
               for error in validate_sfec_payload(payload))


def test_niu_valide_est_normalise_et_transmis():
    payload = db_invoice_to_sfec(_invoice(
        tiers_code="CLI-001",
        nom_tiers="Entreprise OBAMA",
        recipient_type_raw="business",
        recipient_niu=" cg 123456789012345 ",
    ))

    assert payload["recipient_niu"] == "CG123456789012345"
    assert payload["recipient_type"] == "business"
    assert validate_sfec_payload(payload) == []


def test_niu_entreprise_reste_uniquement_taxpayer_niu(monkeypatch):
    monkeypatch.setattr(
        sfec_endpoints,
        "get_company_config",
        lambda: {"tax_number": "CG123456789012345", "currency": "XAF"},
    )

    payload = db_invoice_to_sfec(_invoice(
        tiers_code="BAGUES",
        nom_tiers="Bague's en or",
        recipient_type_raw="business",
        recipient_niu="",
    ))

    assert "recipient_niu" not in payload
    assert payload["taxpayer_niu"] == "CG123456789012345"
    assert any("recipient_niu requis" in error
               for error in validate_sfec_payload(payload))


def test_configuration_des_codes_comptoir_est_prioritaire(monkeypatch):
    monkeypatch.setattr(
        sfec_endpoints,
        "get_config",
        lambda: {"sfec": {
            "counter_customer_codes": ["CPT-X"],
            "counter_name_keywords": [],
        }},
    )

    payload = db_invoice_to_sfec(_invoice(
        tiers_code="CPT-X",
        nom_tiers="Societe X",
        recipient_type_raw="business",
        recipient_niu="",
    ))

    assert payload["recipient_type"] == "individual"
    assert "recipient_niu" not in payload


def test_configuration_des_mots_cles_comptoir(monkeypatch):
    monkeypatch.setattr(
        sfec_endpoints,
        "get_config",
        lambda: {"sfec": {
            "counter_customer_codes": [],
            "counter_name_keywords": ["ANONYME"],
        }},
    )

    payload = db_invoice_to_sfec(_invoice(
        tiers_code="XYZ",
        nom_tiers="Vente anonyme",
        recipient_type_raw="business",
        recipient_niu="",
    ))

    assert payload["recipient_type"] == "individual"
    assert "recipient_niu" not in payload


def test_helper_ne_detecte_pas_un_client_ordinaire():
    assert is_counter_customer("CLI-001", "Entreprise OBAMA") is False
    assert is_counter_customer("CLI-CPT", "Client comptoir") is True
    assert is_counter_customer("", "Client comptoir") is True


def test_payload_sqlite_transmet_le_code_tiers():
    payload = sqlite_invoice_to_sfec(_invoice(
        id=42,
        numero="INV-42",
        tiers_code="CLI-CPT",
        tiers_nom="Client comptoir",
        tiers_type="business",
        tiers_niu="",
        source="pos",
    ))

    assert payload["invoice_id"] == "INV-42"
    assert payload["recipient_type"] == "individual"
    assert "recipient_niu" not in payload


def test_pos_force_individual_pour_le_comptoir():
    tiers, error = pos_engine._resolve_ticket_tiers({
        "tiers_code": "CLI-CPT",
        "tiers_nom": "Client comptoir",
        "tiers_type": "business",
        "tiers_niu": "",
    })

    assert error == ""
    assert tiers["tiers_type"] == "individual"


def test_payload_ne_modifie_pas_le_contact_comptoir():
    sqlite_db.init_database()
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "SELECT code, type, type_raw, niu FROM contacts WHERE code = 'CLI-CPT'"
        )
        row = cur.fetchone()

    if not row:
        pytest.skip("Contact CLI-CPT absent de cette base de test")

    before = dict(row)
    db_invoice_to_sfec(_invoice(
        tiers_code="CLI-CPT",
        nom_tiers="Client comptoir",
        recipient_type_raw="business",
        recipient_niu="",
    ))

    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "SELECT code, type, type_raw, niu FROM contacts WHERE code = 'CLI-CPT'"
        )
        after = dict(cur.fetchone())

    assert after == before
