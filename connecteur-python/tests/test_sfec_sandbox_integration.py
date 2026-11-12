"""Tests d'intégration SFEC Sandbox.

Ces tests envoient de VRAIES requêtes à l'API sandbox SFEC
(`https://sandbox.api.sfec.gouv.cg`) en utilisant la clé `sandbox_api_key`
définie dans `config.json`.

Une implémentation est considérée "juste" uniquement si ces tests passent
(certification retournant 201 Created).

Ils ne s'exécutent que si la variable d'environnement
`TCONNECTOR_TEST_SFEC_SANDBOX=1` est définie, pour éviter des appels
réseau accidentels en CI locale.
"""

import json
import os

import pytest

from app.config.manager import get_config


SANDBOX_URL = "https://sandbox.api.sfec.gouv.cg"


@pytest.fixture(scope="module")
def sandbox_api_key():
    """Récupère la clé sandbox depuis config.json (lecture directe car
    get_config() peut filtrer certaines cles via _deep_merge)."""
    import json
    from pathlib import Path

    config_path = Path(__file__).resolve().parent.parent / "config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pytest.skip(f"config.json introuvable ou invalide: {config_path}")
    sfec_cfg = cfg.get("sfec", {})
    key = sfec_cfg.get("sandbox_api_key")
    if not key:
        pytest.skip("sandbox_api_key non configuree dans config.json (section sfec)")
    return key


@pytest.fixture(scope="module")
def sandbox_headers(sandbox_api_key):
    return {
        "X-API-Key": sandbox_api_key,
        "Content-Type": "application/json",
    }


@pytest.fixture(scope="function")
def unique_id():
    """Génère un ID de facture unique pour éviter les doublons 409."""
    import time
    return str(int(time.time() * 1000))


@pytest.mark.integration
@pytest.mark.sandbox
def test_sandbox_connectivity(sandbox_headers):
    """Vérifie que la clé sandbox est valide (GET une facture existante)."""
    import requests

    resp = requests.get(
        f"{SANDBOX_URL}/api/v1/invoices/ed1120d5-0ca3-4207-b80e-92213ed2a690",
        headers=sandbox_headers,
        timeout=15,
    )
    assert resp.status_code == 200, f"Clé sandbox invalide: {resp.text}"
    data = resp.json()
    assert data["invoice_status"] == "certified"


@pytest.mark.integration
@pytest.mark.sandbox
def test_sandbox_certify_sales_invoice_3pct_tar(sandbox_headers, unique_id):
    """Facture de vente avec taxe supplémentaire 3% (code TAR)."""
    import requests

    payload = {
        "invoice_id": unique_id,
        "invoice_type": "salesInvoice",
        "invoice_subject": "Test taxe 3% avec code TAR",
        "subtotal": 14094340,
        "total_tax_t_amount": 2536981,
        "total_tax_r_amount": 0,
        "total_exempt_amount": 0,
        "total_tax_amount": 2536981,
        "discount_amount": 0,
        "amount_due": 17181000,
        "total_line_discount_amount": 0,
        "additional_cent_tax": 126849,
        "electronic_stamp_duty": 0,
        "total_amount": 17181000,
        "currency": "XAF",
        "recipient_type": "business",
        "recipient_name": "Test Client Sandbox",
        "recipient_niu": "M0000000000000001",
        "recipient_phone": "+242060000000",
        "recipient_email": "test@test.com",
        "is_recipient_taxable": True,
        "payment_method": "bank_transfer",
        "items": [
            {
                "designation": "Produit test 3%",
                "type": "product",
                "unit_price": 14094340,
                "quantity": 1,
                "subtotal": 14094340,
                "discount_amount": 0,
                "amount_after_discount": 14094340,
                "net_amount": 16631321,
                "tax_rate": "18",
                "tax_amount": 2536981,
                "total_amount": 16631321,
            }
        ],
        "additional_taxes": [
            {
                "tax_code": "TAR",
                "tax_label": "Taxe supplementaire 3 %",
                "tax_amount": 422830,
                "tax_rate": 3,
            }
        ],
    }

    resp = requests.post(
        f"{SANDBOX_URL}/api/v1/invoices",
        headers=sandbox_headers,
        json=payload,
        timeout=15,
    )
    assert resp.status_code == 201, f"Certification échouée: {resp.text}"
    data = resp.json()
    assert "certification_date" in data or "signature" in data if "certification_status" in data else True
    assert "certification_date" in data or "signature" in data


@pytest.mark.integration
@pytest.mark.sandbox
def test_sandbox_certify_credit_note(sandbox_headers, unique_id):
    """Avoir (creditNote) avec référence parent et montants positifs."""
    import requests

    # D'abord certifier une facture parent
    parent_id = unique_id + "-parent"
    parent_payload = {
        "invoice_id": parent_id,
        "invoice_type": "salesInvoice",
        "invoice_subject": "Facture parent pour avoir",
        "subtotal": 10000,
        "total_tax_t_amount": 1800,
        "total_tax_r_amount": 0,
        "total_exempt_amount": 0,
        "total_tax_amount": 1800,
        "discount_amount": 0,
        "amount_due": 11890,
        "total_line_discount_amount": 0,
        "additional_cent_tax": 90,
        "electronic_stamp_duty": 0,
        "total_amount": 11890,
        "currency": "XAF",
        "recipient_type": "business",
        "recipient_name": "Test Client Parent",
        "recipient_niu": "M0000000000000001",
        "recipient_phone": "+242060000000",
        "recipient_email": "test@test.com",
        "is_recipient_taxable": True,
        "items": [
            {
                "designation": "Produit parent",
                "type": "product",
                "unit_price": 10000,
                "quantity": 1,
                "subtotal": 10000,
                "discount_amount": 0,
                "amount_after_discount": 10000,
                "net_amount": 11800,
                "tax_rate": "18",
                "tax_amount": 1800,
                "total_amount": 11800,
            }
        ],
        "additional_taxes": [],
    }

    resp = requests.post(
        f"{SANDBOX_URL}/api/v1/invoices",
        headers=sandbox_headers,
        json=parent_payload,
        timeout=15,
    )
    assert resp.status_code == 201, f"Certification parent échouée: {resp.text}"

    # Puis certifier l'avoir qui y fait référence
    payload = {
        "invoice_id": unique_id,
        "invoice_type": "creditNote",
        "invoice_subject": "Avoir test avec référence parent",
        "reference_invoice_id": parent_id,
        "subtotal": 10000,
        "total_tax_t_amount": 1800,
        "total_tax_r_amount": 0,
        "total_exempt_amount": 0,
        "total_tax_amount": 1800,
        "discount_amount": 0,
        "amount_due": 11890,
        "total_line_discount_amount": 0,
        "additional_cent_tax": 90,
        "electronic_stamp_duty": 0,
        "total_amount": 11890,
        "currency": "XAF",
        "recipient_type": "business",
        "recipient_name": "Test Client Avoir",
        "recipient_niu": "M0000000000000001",
        "recipient_phone": "+242060000000",
        "recipient_email": "test@test.com",
        "is_recipient_taxable": True,
        "external_credit_note_number": "AV-SANDBOX-001",
        "original_sfec_invoice_number": parent_id,
        "items": [
            {
                "designation": "Avoir produit test",
                "type": "product",
                "unit_price": 10000,
                "quantity": 1,
                "subtotal": 10000,
                "discount_amount": 0,
                "amount_after_discount": 10000,
                "net_amount": 11800,
                "tax_rate": "18",
                "tax_amount": 1800,
                "total_amount": 11800,
            }
        ],
        "additional_taxes": [],
    }

    resp = requests.post(
        f"{SANDBOX_URL}/api/v1/invoices",
        headers=sandbox_headers,
        json=payload,
        timeout=15,
    )
    assert resp.status_code == 201, f"Certification avoir échouée: {resp.text}"
    data = resp.json()
    assert "certification_date" in data or "signature" in data


@pytest.mark.integration
@pytest.mark.sandbox
def test_sandbox_certify_exoneree(sandbox_headers, unique_id):
    """Facture exonérée (TTC = HT, pas de TVA)."""
    import requests

    payload = {
        "invoice_id": unique_id,
        "invoice_type": "salesInvoice",
        "invoice_subject": "Facture exonérée",
        "subtotal": 250000,
        "total_tax_t_amount": 0,
        "total_tax_r_amount": 0,
        "total_exempt_amount": 250000,
        "total_tax_amount": 0,
        "discount_amount": 0,
        "amount_due": 250000,
        "total_line_discount_amount": 0,
        "additional_cent_tax": 0,
        "electronic_stamp_duty": 0,
        "total_amount": 250000,
        "currency": "XAF",
        "recipient_type": "business",
        "recipient_name": "Test Client Exonéré",
        "recipient_niu": "M0000000000000001",
        "recipient_phone": "+242060000000",
        "recipient_email": "test@test.com",
        "is_recipient_taxable": True,
        "items": [
            {
                "designation": "Produit exonéré",
                "type": "product",
                "unit_price": 250000,
                "quantity": 1,
                "subtotal": 250000,
                "discount_amount": 0,
                "amount_after_discount": 250000,
                "net_amount": 250000,
                "tax_rate": "0",
                "tax_amount": 0,
                "total_amount": 250000,
            }
        ],
        "additional_taxes": [],
    }

    resp = requests.post(
        f"{SANDBOX_URL}/api/v1/invoices",
        headers=sandbox_headers,
        json=payload,
        timeout=15,
    )
    assert resp.status_code == 201, f"Certification exonérée échouée: {resp.text}"
    data = resp.json()
    assert "certification_date" in data or "signature" in data


@pytest.mark.integration
@pytest.mark.sandbox
def test_sandbox_multiple_returns_same_parent(sandbox_headers, unique_id):
    """Plusieurs retours partiels sur la même facture parent."""
    import requests

    parent_id = unique_id

    # D'abord certifier le parent
    parent_payload = {
        "invoice_id": parent_id,
        "invoice_type": "salesInvoice",
        "invoice_subject": "Facture parent pour retours multiples",
        "subtotal": 100000,
        "total_tax_t_amount": 18000,
        "total_tax_r_amount": 0,
        "total_exempt_amount": 0,
        "total_tax_amount": 18000,
        "discount_amount": 0,
        "amount_due": 118900,
        "total_line_discount_amount": 0,
        "additional_cent_tax": 900,
        "electronic_stamp_duty": 0,
        "total_amount": 118900,
        "currency": "XAF",
        "recipient_type": "business",
        "recipient_name": "Test Client Parent",
        "recipient_niu": "M0000000000000001",
        "recipient_phone": "+242060000000",
        "recipient_email": "test@test.com",
        "is_recipient_taxable": True,
        "items": [
            {
                "designation": "Produit parent",
                "type": "product",
                "unit_price": 100000,
                "quantity": 1,
                "subtotal": 100000,
                "discount_amount": 0,
                "amount_after_discount": 100000,
                "net_amount": 118900,
                "tax_rate": "18",
                "tax_amount": 18000,
                "total_amount": 118900,
            }
        ],
        "additional_taxes": [],
    }

    resp = requests.post(
        f"{SANDBOX_URL}/api/v1/invoices",
        headers=sandbox_headers,
        json=parent_payload,
        timeout=15,
    )
    assert resp.status_code == 201, f"Certification parent échouée: {resp.text}"

    # Premier retour partiel
    retour_id1 = unique_id + "-ret1"
    retour1 = {
        "invoice_id": retour_id1,
        "invoice_type": "creditNote",
        "invoice_subject": "Retour partiel 1",
        "reference_invoice_id": parent_id,
        "subtotal": 30000,
        "total_tax_t_amount": 5400,
        "total_tax_r_amount": 0,
        "total_exempt_amount": 0,
        "total_tax_amount": 5400,
        "discount_amount": 0,
        "amount_due": 35700,
        "total_line_discount_amount": 0,
        "additional_cent_tax": 270,
        "electronic_stamp_duty": 0,
        "total_amount": 35700,
        "currency": "XAF",
        "recipient_type": "business",
        "recipient_name": "Test Client Retour 1",
        "recipient_niu": "M0000000000000001",
        "recipient_phone": "+242060000000",
        "recipient_email": "test@test.com",
        "is_recipient_taxable": True,
        "external_credit_note_number": "RET-SANDBOX-001",
        "original_sfec_invoice_number": parent_id,
        "items": [
            {
                "designation": "Retour partiel 1",
                "type": "product",
                "unit_price": 30000,
                "quantity": 1,
                "subtotal": 30000,
                "discount_amount": 0,
                "amount_after_discount": 30000,
                "net_amount": 35460,
                "tax_rate": "18",
                "tax_amount": 5400,
                "total_amount": 35460,
            }
        ],
        "additional_taxes": [],
    }

    resp = requests.post(
        f"{SANDBOX_URL}/api/v1/invoices",
        headers=sandbox_headers,
        json=retour1,
        timeout=15,
    )
    assert resp.status_code == 201, f"Retour 1 échoué: {resp.text}"

    # Deuxième retour partiel sur le même parent (doit être autorisé)
    retour_id2 = unique_id + "-ret2"
    retour2 = {
        "invoice_id": retour_id2,
        "invoice_type": "creditNote",
        "invoice_subject": "Retour partiel 2",
        "reference_invoice_id": parent_id,
        "subtotal": 20000,
        "total_tax_t_amount": 3600,
        "total_tax_r_amount": 0,
        "total_exempt_amount": 0,
        "total_tax_amount": 3600,
        "discount_amount": 0,
        "amount_due": 23800,
        "total_line_discount_amount": 0,
        "additional_cent_tax": 180,
        "electronic_stamp_duty": 0,
        "total_amount": 23800,
        "currency": "XAF",
        "recipient_type": "business",
        "recipient_name": "Test Client Retour 2",
        "recipient_niu": "M0000000000000001",
        "recipient_phone": "+242060000000",
        "recipient_email": "test@test.com",
        "is_recipient_taxable": True,
        "external_credit_note_number": "RET-SANDBOX-002",
        "original_sfec_invoice_number": parent_id,
        "items": [
            {
                "designation": "Retour partiel 2",
                "type": "product",
                "unit_price": 20000,
                "quantity": 1,
                "subtotal": 20000,
                "discount_amount": 0,
                "amount_after_discount": 20000,
                "net_amount": 23600,
                "tax_rate": "18",
                "tax_amount": 3600,
                "total_amount": 23600,
            }
        ],
        "additional_taxes": [],
    }

    resp = requests.post(
        f"{SANDBOX_URL}/api/v1/invoices",
        headers=sandbox_headers,
        json=retour2,
        timeout=15,
    )
    assert resp.status_code == 201, f"Retour 2 échoué: {resp.text}"
    data = resp.json()
    assert "certification_date" in data or "signature" in data
