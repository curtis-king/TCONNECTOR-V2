"""Mocks SFEC en memoire : certif immediate, 0 reseau, 0 sleep.

Patche SfecClient au niveau classe (utilise par engine + dashboard)
et les noms importes par app.sync.engine (is_online, certify_invoice).
"""
from unittest.mock import patch as _patch

# PNG 1x1 valide (signature PNG OK pour _decode_qr_image)
_QR = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
       "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
_SIG = "a" * 64

_patchers = []


def _mk_cert(numero):
    short = "MOCK{}".format(abs(hash(numero)) % 10 ** 14)
    return {
        "certification_number": "MOCK-CERT-{}".format(numero),
        "signature": _SIG,
        "short_signature": short,
        "identifier": short,
        "invoice_number": numero,
        "certification_date": "2026-10-06T12:00:00Z",
        "qr_code": _QR,
    }


def _mk_invoice(uuid_or_numero):
    return {
        "id": uuid_or_numero,
        "invoice_number": "FA-PERF-MOCK",
        "invoice_date": "2026-10-06T12:00:00Z",
        "invoice_type": "salesInvoice",
        "invoice_status": "certified",
        "currency": "XAF",
        "seller_name": "Societe Perf",
        "seller_address": "Brazzaville",
        "seller_niu": "M00000000000000P",
        "seller_rccm": "CG-BZV-2026-B-001",
        "seller_legal_form": "SARL",
        "seller_tax_regime": "REEL",
        "seller_capital": "1000000",
        "buyer_name": "Client Perf",
        "buyer_niu": "M11111111111111P",
        "buyer_address": "Brazzaville",
        "buyer_phone": "+242060000000",
        "payment_method": "Especes",
        "amount_due": 1180.0,
        "total_ht": 1000.0,
        "total_tax_t_amount": 180.0,
        "total_tax18": 180.0,
        "total_tax_r_amount": 0.0,
        "total_tax5": 0.0,
        "total_exempt_amount": 0.0,
        "total_ttc": 1180.0,
        "discount_amount": 0.0,
        "additional_cent_tax": 9.0,
        "electronic_stamp_duty": 0.0,
        "recipient_type": "business",
        "reference_invoice_id": "",
        "items": [{
            "designation": "Article Perf",
            "quantity": 1,
            "unit_price": 1000.0,
            "tax_rate": "18",
            "net_amount": 1180.0,
        }],
        "certification_status": "certified",
        "certification_short_signature": "MOCKSIG123456789012",
        "certification_signature": _SIG,
        "certification_date": "2026-10-06T12:00:00Z",
        "qr_code": _QR,
        "certification_qr_code": _QR,
    }


def certify_invoice(invoice):
    numero = invoice.get("numero", "MOCK")
    return {"success": True, "numero": numero, **_mk_cert(numero)}


def start():
    import app.sync.engine as engine
    from app.integration.sfec.client import SfecClient

    _patchers.append(_patch.object(engine, "is_online", lambda: True))
    _patchers.append(_patch.object(engine, "certify_invoice", certify_invoice))
    _patchers.append(_patch.object(SfecClient, "list_invoices",
                                   lambda self, page=1, page_size=500, **k:
                                   {"invoices": [], "totalPages": 1, "page": page}))
    _patchers.append(_patch.object(SfecClient, "get_invoice",
                                   lambda self, invoice_id: _mk_invoice(invoice_id)))
    _patchers.append(_patch.object(SfecClient, "verify_by_invoice_number",
                                   lambda self, numero: _mk_invoice(numero)))
    _patchers.append(_patch.object(SfecClient, "certify",
                                   lambda self, payload: _mk_cert(payload.get("invoice_id", "MOCK"))))
    for p in _patchers:
        p.start()


def stop():
    while _patchers:
        _patchers.pop().stop()
