"""Impression des factures certifiees : reference article et designation."""
from app.web.routes import dashboard


def _fake_invoice(**overrides):
    invoice = {
        "id": "93e873b3-186a-4498-9ab5-99842be826a9",
        "invoice_number": "FCDFG016",
        "invoice_type": "salesInvoice",
        "invoice_date": "2026-10-29T06:13:43.01Z",
        "seller_invoice_number": None,
        "buyer_name": "Client test",
        "buyer_niu": "",
        "buyer_address": "Adresse test",
        "buyer_phone": "000000000",
        "currency": "XAF",
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
        "total_ht": 588000,
        "total_ttc": 699132,
        "amount_due": 699132,
        "total_tax_t_amount": 105840,
        "total_tax_r_amount": 0,
        "total_exempt_amount": 0,
        "certification_short_signature": "7378C77ABEE2A59402C6",
        "certification_date": "2026-10-29T06:13:43.01Z",
        "certification_qr_code": "",
    }
    invoice.update(overrides)
    return invoice


def _sage_line():
    return {
        "description": "Gravure sur Or ciselé",
        "article_design": "Gravure sur Or ciselé",
        "code_produit": "GRAVURE",
        "quantite": 24,
        "prix_unitaire": 24500,
        "montant_ht": 588000,
        "taux_tva": 18,
    }


def test_normalisation_ligne_sage():
    line = dashboard._normalize_print_line(_sage_line())

    assert line["code_article"] == "GRAVURE"
    assert line["designation"] == "Gravure sur Or ciselé"
    assert line["unite"] == ""


def test_lecture_items_json_et_montant_ht():
    item = dashboard._sfec_invoice_items(_fake_invoice())[0]

    assert item["designation"] == "Gravure sur Or ciselé"
    assert dashboard._sfec_item_ht(item) == 588000


def test_lecture_items_json_chaine():
    invoice = _fake_invoice()
    invoice["items_json"] = (
        '[{"designation": "Article JSON", "subtotal": 1000, '
        '"net_amount": 1180, "tax_amount": 180}]'
    )

    items = dashboard._sfec_invoice_items(invoice)

    assert items[0]["designation"] == "Article JSON"


def test_page_impression_affiche_reference_et_designation(client, monkeypatch):
    invoice = _fake_invoice()

    class FakeSfecClient:
        def get_invoice(self, _invoice_id):
            return invoice

    monkeypatch.setattr(dashboard, "SfecClient", FakeSfecClient)
    monkeypatch.setattr(
        dashboard, "_print_local_lines",
        lambda numero, type_doc="vente": (
            {"numero": numero, "source": "sage", "reference": "autre test",
             "tiers_code": "BAGUES"},
            [],
        ),
    )
    monkeypatch.setattr(
        dashboard, "_print_sage_lines",
        lambda numero, type_doc="vente", domaine=None, sage_type=None: [
            _sage_line()
        ],
    )

    response = client.get(
        "/certified/93e873b3-186a-4498-9ab5-99842be826a9/print"
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Gravure sur Or ciselé" in html
    assert html.count("GRAVURE") >= 2
    assert "588 000" in html
    assert "693 840" not in html


def test_page_impression_repli_sur_items_json(client, monkeypatch):
    invoice = _fake_invoice()

    class FakeSfecClient:
        def get_invoice(self, _invoice_id):
            return invoice

    monkeypatch.setattr(dashboard, "SfecClient", FakeSfecClient)
    monkeypatch.setattr(
        dashboard, "_print_local_lines",
        lambda numero, type_doc="vente": (None, []),
    )
    monkeypatch.setattr(
        dashboard, "_print_sage_lines",
        lambda numero, type_doc="vente", domaine=None, sage_type=None: [],
    )

    response = client.get(
        "/certified/93e873b3-186a-4498-9ab5-99842be826a9/print"
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Gravure sur Or ciselé" in html
    assert "588 000" in html
