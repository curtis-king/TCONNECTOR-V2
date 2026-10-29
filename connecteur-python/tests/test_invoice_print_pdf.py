"""Impression PDF des factures issues de Sage / certifiees SFEC."""
from io import BytesIO

from pypdf import PdfReader

from app.domain import pdf as pdf_generator
from app.domain.invoice_print_data import enrich_invoice_for_print
from app.web.routes import billing

PREFIX = "TEST-PRINT-PDF-"


def _sfec_invoice():
    return {
        "buyer_name": "Bague's en or",
        "buyer_address": "Rue tête dOr",
        "buyer_niu": "546789AZETOPDSEDD",
        "buyer_phone": "06 65 87 00 00",
        "invoice_date": "2026-10-28",
        "items_json": [{
            "designation": "Gravure sur Or ciselé",
            "amount_after_discount": 588000,
            "subtotal": 588000,
            "net_amount": 693840,
            "tax_amount": 105840,
            "quantity": 24,
            "unit_price": 24500,
            "tax_rate": "18.0",
        }],
    }


def _sage_line():
    return {
        "description": "Gravure sur Or ciselé",
        "article_design": "Gravure sur Or ciselé",
        "code_produit": "GRAVURE",
        "quantite": 24,
        "prix_unitaire": 24500,
        "montant_ht": 588000,
        "montant_tva": 105840,
        "montant_ttc": 699132,
        "taux_tva": 18,
    }


def _local_invoice():
    return {
        "id": 44,
        "numero": "FCDFG016",
        "date_facture": "2026-10-28",
        "reference": "autre test",
        "tiers_code": "BAGUES",
        "tiers_nom": "",
        "tiers_adresse": "",
        "tiers_niu": "",
        "tiers_telephone": "",
        "source": "sage",
        "type_doc": "vente",
        "sage_domaine": 0,
        "sage_type": 6,
        "sage_piece": "FCDFG016",
        "sfec_id": "93e873b3-186a-4498-9ab5-99842be826a9",
        "sfec_num_certif": "7378C77ABEE2A59402C6",
        "sfec_date_certif": "2026-10-29T06:13:43.01Z",
        "montant_ht": 588000,
        "montant_tva": 105840,
        "montant_ttc": 699132,
        "montant_restant": 699132,
        "montant_ht_brut": 588000,
        "total_tax_t_amount": 105840,
        "total_tax_r_amount": 0,
        "total_exempt_amount": 0,
        "additional_cent_tax": 5292,
        "devise": "XAF",
    }


class _FakeSfecClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload or _sfec_invoice()
        self.error = error

    def get_invoice(self, _invoice_id):
        if self.error:
            raise self.error
        return self.payload


def test_enrichissement_utilise_sfec_et_lignes_sage():
    calls = []

    def sage_fetcher(domaine, type_doc, piece):
        calls.append((domaine, type_doc, piece))
        return [_sage_line()]

    enriched = enrich_invoice_for_print(
        _local_invoice(),
        sfec_client=_FakeSfecClient(),
        sage_lines_fetcher=sage_fetcher,
        contact_fetcher=lambda _code: (_ for _ in ()).throw(
            AssertionError("le contact Sage ne doit pas être lu si SFEC fournit le nom")
        ),
    )

    assert calls == [(0, 6, "FCDFG016")]
    assert enriched["tiers_nom"] == "Bague's en or"
    assert enriched["tiers_niu"] == "546789AZETOPDSEDD"
    assert enriched["reference"] == "GRAVURE"
    line = enriched["lignes"][0]
    assert line["code_article"] == "GRAVURE"
    assert line["designation"] == "Gravure sur Or ciselé"
    assert line["montant_ht"] == 588000
    assert line["montant_tva"] == 105840


def test_repli_sage_si_sfec_indisponible():
    enriched = enrich_invoice_for_print(
        _local_invoice(),
        sfec_client=_FakeSfecClient(error=RuntimeError("SFEC indisponible")),
        sage_lines_fetcher=lambda *_args: [_sage_line()],
        contact_fetcher=lambda code: {
            "code": code,
            "nom": "Client Sage",
            "adresse": "Adresse Sage",
            "telephone": "0102030405",
            "numero_fiscal": "NIU-SAGE",
            "email": "sage@example.com",
        },
    )

    assert enriched["tiers_nom"] == "Client Sage"
    assert enriched["tiers_adresse"] == "Adresse Sage"
    assert enriched["reference"] == "GRAVURE"


def test_pdf_enrichi_contient_reference_designation_et_client():
    enriched = enrich_invoice_for_print(
        _local_invoice(),
        sfec_client=_FakeSfecClient(),
        sage_lines_fetcher=lambda *_args: [_sage_line()],
        contact_fetcher=lambda _code: {},
    )

    pdf = pdf_generator.generate_invoice_pdf(enriched)
    assert pdf
    text = "\n".join(
        (page.extract_text() or "") for page in PdfReader(BytesIO(pdf)).pages
    )

    assert "GRAVURE" in text
    assert "Gravure sur Or ciselé" in text
    assert "Bague's en or" in text
    assert "Client :   -" not in text
    assert "693 840" not in text


def test_route_pdf_utilise_les_donnees_impression(client, monkeypatch):
    csrf = client.get("/api/csrf").get_json()["token"]
    response = client.post(
        "/api/invoices",
        json={
            "numero": PREFIX + "PDF",
            "tiers_nom": "Client test",
            "statut": "valide",
            "lignes": [{"designation": "Ligne test", "quantite": 1,
                        "prix_unitaire": 1000, "taux_tva": 18}],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.get_json()
    invoice_id = response.get_json()["id"]

    def fake_enrich(inv):
        enriched = dict(inv)
        enriched.update({
            "tiers_nom": "Client PDF",
            "reference": "GRAVURE",
            "sfec_num_certif": "",
            "lignes": [{
                "code_article": "GRAVURE",
                "designation": "Gravure sur Or ciselé",
                "quantite": 24,
                "prix_unitaire": 24500,
                "montant_ht": 588000,
                "montant_tva": 105840,
                "montant_ttc": 699132,
                "taux_tva": 18,
            }],
        })
        return enriched

    monkeypatch.setattr(billing, "enrich_invoice_for_print", fake_enrich)

    response = client.get("/api/invoices/{}/pdf".format(invoice_id))
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    text = "\n".join(
        (page.extract_text() or "")
        for page in PdfReader(BytesIO(response.data)).pages
    )
    assert "GRAVURE" in text
    assert "Gravure sur Or ciselé" in text
    assert "Client PDF" in text
